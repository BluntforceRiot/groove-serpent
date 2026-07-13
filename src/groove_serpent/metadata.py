"""Opt-in online release metadata and cover-art helpers.

Importing this module never performs network I/O.  Callers must explicitly
construct a client and invoke a lookup or download method.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, BinaryIO, Mapping, cast

from . import __user_agent__
from .errors import GrooveSerpentError


DEFAULT_USER_AGENT = __user_agent__
MAX_JSON_BYTES = 10 * 1024 * 1024
MAX_ARTWORK_BYTES = 25 * 1024 * 1024

_SIDE_NUMBER = re.compile(r"^\s*([A-Za-z]{1,2})\s*[-.]?\s*0*(\d+)\s*$")
_IMAGE_TYPES = {
    "image/jpeg": ("image/jpeg", ".jpg"),
    "image/jpg": ("image/jpeg", ".jpg"),
    "image/png": ("image/png", ".png"),
}


class MetadataLookupError(GrooveSerpentError):
    """A user-facing failure while looking up metadata or cover artwork."""


def _validate_user_agent(value: str) -> str:
    user_agent = str(value).strip()
    if not user_agent or len(user_agent) > 256 or "\r" in user_agent or "\n" in user_agent:
        raise ValueError("user_agent must be a non-empty, single-line application identifier.")
    return user_agent


def _validated_uuid(value: str, label: str = "release ID") -> str:
    if not isinstance(value, str) or not value.strip():
        raise MetadataLookupError(f"The {label} must be a MusicBrainz UUID.")
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError) as exc:
        raise MetadataLookupError(f"The {label} is not a valid MusicBrainz UUID.") from exc


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _artist_credit(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    result: list[str] = []
    for part in value:
        if isinstance(part, str):
            result.append(part)
            continue
        if not isinstance(part, Mapping):
            continue
        artist = part.get("artist")
        artist_name = artist.get("name", "") if isinstance(artist, Mapping) else ""
        name = str(part.get("name") or artist_name or "").strip()
        if name:
            result.append(name)
        join_phrase = part.get("joinphrase")
        if isinstance(join_phrase, str):
            result.append(join_phrase)
    return "".join(result).strip()


def _first_label(release: Mapping[str, Any]) -> tuple[str, str]:
    entries = release.get("label-info")
    if not isinstance(entries, list):
        return "", ""
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        label = entry.get("label")
        name = label.get("name", "") if isinstance(label, Mapping) else ""
        catalog_number = entry.get("catalog-number", "")
        if name or catalog_number:
            return str(name or "").strip(), str(catalog_number or "").strip()
    return "", ""


def _media_formats(release: Mapping[str, Any]) -> list[str]:
    media = release.get("media")
    if not isinstance(media, list):
        return []
    formats: list[str] = []
    for medium in media:
        if not isinstance(medium, Mapping):
            continue
        value = str(medium.get("format") or "").strip()
        if value and value not in formats:
            formats.append(value)
    return formats


def _track_count(release: Mapping[str, Any]) -> int:
    media = release.get("media")
    if isinstance(media, list):
        counts = [
            _safe_int(medium.get("track-count"))
            for medium in media
            if isinstance(medium, Mapping)
        ]
        if any(counts):
            return sum(counts)
    return _safe_int(release.get("track-count"))


def _cover_art_summary(release: Mapping[str, Any], release_id: str) -> dict[str, Any]:
    value = release.get("cover-art-archive")
    archive = value if isinstance(value, Mapping) else {}
    available = bool(archive.get("artwork"))
    return {
        "available": available,
        "front": bool(archive.get("front")),
        "back": bool(archive.get("back")),
        "count": _safe_int(archive.get("count")),
        "metadata_url": (
            f"https://coverartarchive.org/release/{release_id}" if available else ""
        ),
    }


def _http_error_message(service: str, error: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        payload = error.read(512).decode("utf-8", errors="replace").strip()
        if payload:
            detail = f" Response: {payload}"
    except (OSError, AttributeError):
        pass
    return f"{service} returned HTTP {error.code} ({error.reason}).{detail}"


def _read_json(response: BinaryIO, service: str) -> Any:
    status = getattr(response, "status", 200)
    if not 200 <= int(status) < 300:
        raise MetadataLookupError(f"{service} returned an unexpected HTTP status ({status}).")
    raw = response.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise MetadataLookupError(f"{service} returned an unexpectedly large response.")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MetadataLookupError(f"{service} returned invalid JSON.") from exc


class MusicBrainzClient:
    """Small dependency-free client for explicit MusicBrainz release lookups.

    The one-request-per-second slot is shared by client instances so creating a
    second client cannot accidentally bypass MusicBrainz's application rate.
    """

    BASE_URL = "https://musicbrainz.org/ws/2"
    REQUEST_INTERVAL_SECONDS = 1.0
    _rate_lock = threading.Lock()
    _next_request_by_origin: dict[str, float] = {}

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 10.0,
        base_url: str = BASE_URL,
    ) -> None:
        self.user_agent = _validate_user_agent(user_agent)
        try:
            self.timeout = float(timeout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("timeout must be a positive number of seconds.") from exc
        if self.timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds.")
        self.base_url = str(base_url).rstrip("/")
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP or HTTPS URL.")
        self._rate_origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"

    def _monotonic(self) -> float:
        return time.monotonic()

    def _sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def _wait_for_rate_limit(self) -> None:
        with MusicBrainzClient._rate_lock:
            now = self._monotonic()
            next_request = MusicBrainzClient._next_request_by_origin.get(
                self._rate_origin, now
            )
            delay = next_request - now
            if delay > 0:
                self._sleep(delay)
                now = self._monotonic()
            MusicBrainzClient._next_request_by_origin[self._rate_origin] = (
                max(now, next_request) + self.REQUEST_INTERVAL_SECONDS
            )

    def _open(self, request: urllib.request.Request) -> BinaryIO:
        return cast(BinaryIO, urllib.request.urlopen(request, timeout=self.timeout))

    def _request_json(self, path: str, params: Mapping[str, Any]) -> Any:
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
        )
        self._wait_for_rate_limit()
        try:
            with self._open(request) as response:
                return _read_json(response, "MusicBrainz")
        except urllib.error.HTTPError as exc:
            raise MetadataLookupError(_http_error_message("MusicBrainz", exc)) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise MetadataLookupError(f"Could not reach MusicBrainz: {reason}") from exc

    def search_releases(
        self, artist: str, album: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Search releases and return compact, JSON-ready choices.

        Vinyl media are deliberately sorted ahead of non-vinyl results; within
        each group MusicBrainz's match score remains the primary ordering.
        """

        artist = str(artist).strip()
        album = str(album).strip()
        if not artist or not album:
            raise MetadataLookupError("Artist and album are required for release search.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise MetadataLookupError("Release search limit must be between 1 and 100.")

        def quote(value: str) -> str:
            return value.replace("\\", "\\\\").replace('"', '\\"')

        payload = self._request_json(
            "release/",
            {
                "query": f'artist:"{quote(artist)}" AND release:"{quote(album)}"',
                "fmt": "json",
                "limit": limit,
            },
        )
        if not isinstance(payload, Mapping) or not isinstance(payload.get("releases"), list):
            raise MetadataLookupError("MusicBrainz returned an unexpected release-search response.")

        results = [
            self._simplify_search_release(item)
            for item in payload["releases"]
            if isinstance(item, Mapping) and item.get("id")
        ]
        results.sort(
            key=lambda item: (
                not any("vinyl" in value.casefold() for value in item["formats"]),
                -item["score"],
                item["date"],
                item["title"].casefold(),
                item["id"],
            )
        )
        return results

    @staticmethod
    def _simplify_search_release(release: Mapping[str, Any]) -> dict[str, Any]:
        release_id = str(release.get("id") or "")
        label, catalog_number = _first_label(release)
        release_group = release.get("release-group")
        release_group_id = (
            str(release_group.get("id") or "")
            if isinstance(release_group, Mapping)
            else ""
        )
        cover_art = _cover_art_summary(release, release_id)
        return {
            "id": release_id,
            "title": str(release.get("title") or ""),
            "artist": _artist_credit(release.get("artist-credit")),
            "date": str(release.get("date") or ""),
            "country": str(release.get("country") or ""),
            "score": max(0, min(100, _safe_int(release.get("score")))),
            "status": str(release.get("status") or ""),
            "formats": _media_formats(release),
            "track_count": _track_count(release),
            "barcode": str(release.get("barcode") or ""),
            "label": label,
            "catalog_number": catalog_number,
            "release_group_id": release_group_id,
            "has_artwork": cover_art["available"],
        }

    def get_release(self, release_id: str) -> dict[str, Any]:
        """Fetch a release with recordings and stable whole-medium/side choices."""

        normalized_id = _validated_uuid(release_id)
        payload = self._request_json(
            f"release/{normalized_id}",
            {
                "inc": "recordings+artist-credits+release-groups+genres+labels",
                "fmt": "json",
            },
        )
        if not isinstance(payload, Mapping) or str(payload.get("id") or "") != normalized_id:
            raise MetadataLookupError("MusicBrainz returned an unexpected release response.")
        return self._simplify_release(payload)

    @staticmethod
    def _simplify_release(release: Mapping[str, Any]) -> dict[str, Any]:
        release_id = str(release.get("id") or "")
        release_artist = _artist_credit(release.get("artist-credit"))
        label, catalog_number = _first_label(release)
        media: list[dict[str, Any]] = []
        raw_media = release.get("media")
        if isinstance(raw_media, list):
            for fallback_position, raw_medium in enumerate(raw_media, start=1):
                if not isinstance(raw_medium, Mapping):
                    continue
                medium_position = _safe_int(raw_medium.get("position"), fallback_position)
                if medium_position < 1:
                    medium_position = fallback_position
                tracks: list[dict[str, Any]] = []
                raw_tracks = raw_medium.get("tracks")
                if isinstance(raw_tracks, list):
                    for fallback_track_position, raw_track in enumerate(raw_tracks, start=1):
                        if not isinstance(raw_track, Mapping):
                            continue
                        recording = raw_track.get("recording")
                        recording = recording if isinstance(recording, Mapping) else {}
                        number = str(raw_track.get("number") or fallback_track_position).strip()
                        side_match = _SIDE_NUMBER.fullmatch(number)
                        side = side_match.group(1).upper() if side_match else ""
                        side_position = int(side_match.group(2)) if side_match else None
                        track_artist = (
                            _artist_credit(raw_track.get("artist-credit"))
                            or _artist_credit(recording.get("artist-credit"))
                            or release_artist
                        )
                        duration_ms = _optional_int(
                            raw_track.get("length", recording.get("length"))
                        )
                        title = str(
                            raw_track.get("title") or recording.get("title") or ""
                        ).strip()
                        tracks.append(
                            {
                                "position": max(
                                    1,
                                    _safe_int(
                                        raw_track.get("position"), fallback_track_position
                                    ),
                                ),
                                "number": number,
                                "title": title,
                                "artist": track_artist,
                                "duration_ms": duration_ms,
                                "duration_seconds": (
                                    round(duration_ms / 1000.0, 3)
                                    if duration_ms is not None
                                    else None
                                ),
                                "recording_id": str(recording.get("id") or ""),
                                "track_id": str(raw_track.get("id") or ""),
                                "side": side,
                                "side_position": side_position,
                            }
                        )

                sides: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
                is_vinyl = "vinyl" in str(raw_medium.get("format") or "").casefold()
                for track in tracks:
                    if is_vinyl and track["side"]:
                        sides.setdefault(track["side"], []).append(track)
                medium = {
                    "position": medium_position,
                    "title": str(raw_medium.get("title") or ""),
                    "format": str(raw_medium.get("format") or ""),
                    "track_count": len(tracks),
                    "tracks": tracks,
                    "sides": [
                        {
                            "side": side,
                            "track_count": len(side_tracks),
                            "tracks": side_tracks,
                        }
                        for side, side_tracks in sides.items()
                    ],
                }
                media.append(medium)

        selections = _build_track_selections(media)
        release_group = release.get("release-group")
        release_group = release_group if isinstance(release_group, Mapping) else {}
        genre_values: list[str] = []
        for owner in (release, release_group):
            genres = owner.get("genres")
            if not isinstance(genres, list):
                continue
            for genre in genres:
                if not isinstance(genre, Mapping):
                    continue
                name = str(genre.get("name") or "").strip()
                if name and name not in genre_values:
                    genre_values.append(name)
        artwork = _cover_art_summary(release, release_id)
        formats: list[str] = []
        for medium in media:
            medium_format = str(medium.get("format") or "")
            if medium_format and medium_format not in formats:
                formats.append(medium_format)
        return {
            "id": release_id,
            "title": str(release.get("title") or ""),
            "artist": release_artist,
            "date": str(release.get("date") or ""),
            "country": str(release.get("country") or ""),
            "status": str(release.get("status") or ""),
            "barcode": str(release.get("barcode") or ""),
            "label": label,
            "catalog_number": catalog_number,
            "release_group_id": str(release_group.get("id") or ""),
            "genres": genre_values,
            "formats": formats,
            "track_count": sum(medium["track_count"] for medium in media),
            "has_artwork": artwork["available"],
            "artwork": artwork,
            "media": media,
            "selections": selections,
        }


def _build_track_selections(media: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selections: list[dict[str, Any]] = []
    multiple_media = len(media) > 1
    for medium in media:
        position = _safe_int(medium.get("position"), 1)
        medium_title = str(medium.get("title") or "").strip()
        medium_label = medium_title or f"Medium {position}"
        if not multiple_media and not medium_title:
            medium_label = "Complete release"
        for side in medium.get("sides", []):
            if not isinstance(side, Mapping):
                continue
            side_name = str(side.get("side") or "").upper()
            side_tracks = side.get("tracks")
            if not side_name or not isinstance(side_tracks, list):
                continue
            prefix = f"{medium_label} · " if multiple_media or medium_title else ""
            selections.append(
                {
                    "key": f"medium:{position}:side:{side_name}",
                    "kind": "side",
                    "label": f"{prefix}Side {side_name}",
                    "medium_position": position,
                    "medium_title": medium_title,
                    "format": str(medium.get("format") or ""),
                    "side": side_name,
                    "track_count": len(side_tracks),
                    "tracks": side_tracks,
                }
            )
        all_tracks = medium.get("tracks")
        all_tracks = all_tracks if isinstance(all_tracks, list) else []
        selections.append(
            {
                "key": f"medium:{position}:all",
                "kind": "medium",
                "label": medium_label,
                "medium_position": position,
                "medium_title": medium_title,
                "format": str(medium.get("format") or ""),
                "side": None,
                "track_count": len(all_tracks),
                "tracks": all_tracks,
            }
        )
    if multiple_media:
        release_tracks: list[dict[str, Any]] = []
        release_formats: list[str] = []
        for medium in media:
            tracks = medium.get("tracks")
            if isinstance(tracks, list):
                release_tracks.extend(
                    dict(track) for track in tracks if isinstance(track, Mapping)
                )
            medium_format = str(medium.get("format") or "").strip()
            if medium_format and medium_format not in release_formats:
                release_formats.append(medium_format)
        if release_tracks:
            selections.append(
                {
                    "key": "release:all",
                    "kind": "release",
                    "label": "Complete release",
                    "medium_position": "",
                    "medium_title": "",
                    "format": ", ".join(release_formats),
                    "side": None,
                    "track_count": len(release_tracks),
                    "tracks": release_tracks,
                }
            )
    return selections


def find_track_selections(
    details: Mapping[str, Any],
    preferred_side: str | None = None,
    expected_count: int | None = None,
) -> list[dict[str, Any]]:
    """Return stable release track choices ranked for the recorded source.

    A choice matching both the expected count and requested side ranks first.
    An exact count then outranks an inexact preferred-side choice, which avoids
    silently forcing the wrong number of tracks onto a recording.
    """

    if not isinstance(details, Mapping):
        raise MetadataLookupError("Release details must be an object.")
    if expected_count is not None and (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 1
    ):
        raise MetadataLookupError("Expected track count must be a positive integer.")
    preferred = str(preferred_side or "").strip().upper()
    if preferred.startswith("SIDE "):
        preferred = preferred[5:].strip()

    raw_selections = details.get("selections")
    if not isinstance(raw_selections, list):
        raw_media = details.get("media")
        raw_selections = _build_track_selections(raw_media if isinstance(raw_media, list) else [])
    selections = [dict(item) for item in raw_selections if isinstance(item, Mapping)]

    def rank(item: Mapping[str, Any]) -> tuple[Any, ...]:
        count = _safe_int(item.get("track_count"))
        side = str(item.get("side") or "").upper()
        count_match = expected_count is not None and count == expected_count
        side_match = bool(preferred) and side == preferred
        both_match = count_match and side_match
        delta = abs(count - expected_count) if expected_count is not None else 0
        return (
            not both_match,
            not count_match if expected_count is not None else False,
            not side_match if preferred else False,
            item.get("kind") != "side",
            delta,
            _safe_int(item.get("medium_position"), 1),
            str(item.get("key") or ""),
        )

    selections.sort(key=rank)
    return selections


class CoverArtArchiveClient:
    """Resolve and atomically store Cover Art Archive front artwork."""

    BASE_URL = "https://coverartarchive.org"

    def __init__(
        self,
        project_root: Path | str,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 15.0,
        artwork_folder: str = "artwork",
        max_bytes: int = MAX_ARTWORK_BYTES,
        base_url: str = BASE_URL,
    ) -> None:
        self.user_agent = _validate_user_agent(user_agent)
        try:
            self.timeout = float(timeout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("timeout must be a positive number of seconds.") from exc
        if self.timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds.")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer.")
        if max_bytes > MAX_ARTWORK_BYTES:
            raise ValueError("Artwork downloads may not exceed 25 MB.")
        self.max_bytes = max_bytes
        self.project_root = Path(project_root).expanduser().resolve()
        folder = Path(artwork_folder)
        if folder.is_absolute() or ".." in folder.parts:
            raise ValueError("artwork_folder must stay inside the project directory.")
        self.artwork_dir = (self.project_root / folder).resolve()
        try:
            self.artwork_dir.relative_to(self.project_root)
        except ValueError as exc:
            raise ValueError("artwork_folder must stay inside the project directory.") from exc
        self.base_url = str(base_url).rstrip("/")
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP or HTTPS URL.")

    def _open(self, request: urllib.request.Request) -> BinaryIO:
        return cast(BinaryIO, urllib.request.urlopen(request, timeout=self.timeout))

    def _request_json(self, path: str) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}/{path.lstrip('/')}",
            headers={
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
        )
        try:
            with self._open(request) as response:
                return _read_json(response, "Cover Art Archive")
        except urllib.error.HTTPError as exc:
            raise MetadataLookupError(
                _http_error_message("Cover Art Archive", exc)
            ) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise MetadataLookupError(
                f"Could not reach Cover Art Archive: {reason}"
            ) from exc

    def _resolve_front_art(
        self,
        path: str,
        *,
        identifier_key: str,
        identifier: str,
    ) -> dict[str, Any]:
        payload = self._request_json(path)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("images"), list):
            raise MetadataLookupError("Cover Art Archive returned an unexpected response.")
        candidates = [
            image
            for image in payload["images"]
            if isinstance(image, Mapping) and image.get("front") is True
        ]
        if not candidates:
            raise MetadataLookupError("No front cover is available for this release.")
        candidates.sort(key=lambda image: not bool(image.get("approved")))
        image = candidates[0]
        urls: dict[str, str] = {}
        original = image.get("image")
        if isinstance(original, str) and original:
            urls["original"] = self._validated_art_url(original)
        thumbnails = image.get("thumbnails")
        if isinstance(thumbnails, Mapping):
            for size in ("1200", "500"):
                value = thumbnails.get(size)
                if isinstance(value, str) and value:
                    urls[size] = self._validated_art_url(value)
        if "original" not in urls:
            raise MetadataLookupError("The front-cover record has no downloadable image URL.")
        return {
            identifier_key: identifier,
            "storage_id": identifier,
            "artwork_id": str(image.get("id") or ""),
            "approved": bool(image.get("approved")),
            "comment": str(image.get("comment") or ""),
            "source_url": urls["original"],
            "urls": urls,
        }

    def resolve_front_art(self, release_id: str) -> dict[str, Any]:
        """Return available original/1200/500 URLs for a release's front cover."""

        normalized_id = _validated_uuid(release_id)
        return self._resolve_front_art(
            f"release/{normalized_id}",
            identifier_key="release_id",
            identifier=normalized_id,
        )

    def resolve_release_group_front_art(self, release_group_id: str) -> dict[str, Any]:
        """Return front artwork inherited from another release in a release group."""

        normalized_id = _validated_uuid(release_group_id, "release group ID")
        return self._resolve_front_art(
            f"release-group/{normalized_id}",
            identifier_key="release_group_id",
            identifier=normalized_id,
        )

    @staticmethod
    def _validated_art_url(value: str) -> str:
        parsed = urllib.parse.urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        if parsed.scheme not in {"http", "https"} or not (
            hostname == "coverartarchive.org" or hostname.endswith(".coverartarchive.org")
        ):
            raise MetadataLookupError("Cover Art Archive returned an unsafe image URL.")
        if parsed.scheme == "http":
            parsed = parsed._replace(scheme="https")
        return urllib.parse.urlunsplit(parsed)

    @staticmethod
    def _validate_final_art_url(value: str) -> None:
        parsed = urllib.parse.urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        trusted = (
            hostname == "coverartarchive.org"
            or hostname.endswith(".coverartarchive.org")
            or hostname == "archive.org"
            or hostname.endswith(".archive.org")
        )
        if parsed.scheme != "https" or not trusted:
            raise MetadataLookupError("Artwork download redirected to an unsafe location.")

    @staticmethod
    def _validate_image_signature(mime_type: str, prefix: bytes) -> None:
        valid = False
        if mime_type == "image/jpeg":
            valid = prefix.startswith(b"\xff\xd8\xff")
        elif mime_type == "image/png":
            valid = prefix.startswith(b"\x89PNG\r\n\x1a\n")
        if not valid:
            raise MetadataLookupError("Artwork data does not match its declared image type.")

    def download_front_art(
        self, release_id: str, *, size: str = "1200"
    ) -> dict[str, Any]:
        """Download front art into ``artwork/`` and return portable file metadata."""

        metadata = self.resolve_front_art(release_id)
        return self._download_resolved_front_art(metadata, size=size)

    def download_release_group_front_art(
        self, release_group_id: str, *, size: str = "1200"
    ) -> dict[str, Any]:
        """Download release-group fallback art without changing release behavior."""

        metadata = self.resolve_release_group_front_art(release_group_id)
        return self._download_resolved_front_art(metadata, size=size)

    def _download_resolved_front_art(
        self, metadata: Mapping[str, Any], *, size: str
    ) -> dict[str, Any]:
        """Download a previously resolved release or release-group front cover."""

        if size not in {"1200", "500", "original"}:
            raise MetadataLookupError("Artwork size must be 1200, 500, or original.")
        urls = metadata["urls"]
        selected_size = size if size in urls else "original"
        source_url = urls[selected_size]
        request = urllib.request.Request(
            source_url,
            headers={
                "Accept": "image/jpeg, image/png",
                "User-Agent": self.user_agent,
            },
        )
        try:
            response = self._open(request)
        except urllib.error.HTTPError as exc:
            raise MetadataLookupError(
                _http_error_message("Cover Art Archive", exc)
            ) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise MetadataLookupError(f"Could not download cover artwork: {reason}") from exc

        temporary_path: Path | None = None
        try:
            with response:
                status = getattr(response, "status", 200)
                if not 200 <= int(status) < 300:
                    raise MetadataLookupError(
                        f"Cover Art Archive returned an unexpected HTTP status ({status})."
                    )
                final_url_getter = getattr(response, "geturl", None)
                final_url = final_url_getter() if callable(final_url_getter) else source_url
                self._validate_final_art_url(str(final_url))
                headers = getattr(response, "headers", {})
                raw_content_type = str(headers.get("Content-Type", ""))
                content_type = raw_content_type.split(";", 1)[0].strip().casefold()
                image_type = _IMAGE_TYPES.get(content_type)
                if image_type is None:
                    raise MetadataLookupError(
                        "Cover artwork must be a JPEG or PNG image."
                    )
                mime_type, extension = image_type
                content_length = _optional_int(headers.get("Content-Length"))
                if content_length is not None and content_length > self.max_bytes:
                    raise MetadataLookupError("Cover artwork exceeds the 25 MB download limit.")

                self.artwork_dir.mkdir(parents=True, exist_ok=True)
                resolved_artwork_dir = self.artwork_dir.resolve()
                try:
                    resolved_artwork_dir.relative_to(self.project_root)
                except ValueError as exc:
                    raise MetadataLookupError(
                        "The artwork folder no longer points inside the project."
                    ) from exc
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".cover-", suffix=".tmp", dir=resolved_artwork_dir
                )
                temporary_path = Path(temporary_name)
                digest = hashlib.sha256()
                total = 0
                prefix = b""
                with os.fdopen(descriptor, "wb") as output:
                    while True:
                        chunk = response.read(min(64 * 1024, self.max_bytes - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise MetadataLookupError(
                                "Cover artwork exceeds the 25 MB download limit."
                            )
                        if len(prefix) < 16:
                            prefix += chunk[: 16 - len(prefix)]
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                self._validate_image_signature(mime_type, prefix)
                storage_id = str(
                    metadata.get("storage_id")
                    or metadata.get("release_id")
                    or metadata.get("release_group_id")
                    or ""
                )
                storage_id = _validated_uuid(storage_id, "artwork owner ID")
                destination = resolved_artwork_dir / (
                    f"{storage_id}-front-{selected_size}{extension}"
                )
                os.replace(temporary_path, destination)
                temporary_path = None
        except MetadataLookupError:
            raise
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise MetadataLookupError(f"Could not save cover artwork: {reason}") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

        relative_path = destination.relative_to(self.project_root).as_posix()
        return {
            "relative_path": relative_path,
            "source_url": source_url,
            "mime_type": mime_type,
            "sha256": digest.hexdigest(),
            "size_bytes": total,
            "requested_size": size,
            "selected_size": selected_size,
        }
