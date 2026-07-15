"""Evidence-first album release identification without network or write authority.

This module aggregates already-collected per-track acoustic recognition results.
It does not contact AcoustID, MusicBrainz, or an artwork service, and it never
changes an album or side project.  Every observation is bound to the exact
album, project, editable state, source, speed state, and track range that was
current when the observation was captured.  Proposal creation recaptures those
identities and fails closed if any binding is stale.

A MusicBrainz release identifier identifies a database release.  It does not
prove that a physical copy is that pressing.  Label, catalog number, country,
date, format, barcode, artwork, and especially matrix/runout evidence remain
owner-review facts.  Manual candidates are carried as unranked review input and
cannot authorize metadata, artwork, topology, speed, restoration, or publication
changes.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date as calendar_date
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from . import __version__
from .album import (
    AlbumProject,
    load_album_project_with_sha256,
    project_speed_state,
    resolve_album_reference,
)
from .album_publication_policy import speed_correction_details
from .errors import ExportError, ProjectValidationError
from .models import MAX_TRACKS, Project, Track, resolve_source_path
from .project_io import load_project_with_sha256
from .publication import (
    FileReceipt,
    assert_file_receipt,
    canonical_json_sha256,
    capture_file_receipt,
)
from .recognition import RECOGNITION_SPEED_TRANSFORM, RecognitionMatch
from .validation import strict_finite_number


ALBUM_IDENTIFICATION_EVIDENCE_SCHEMA = (
    "groove-serpent.album-identification-track-evidence/2"
)
ALBUM_IDENTIFICATION_PROPOSAL_SCHEMA = "groove-serpent.album-identification-proposal/2"
ALBUM_IDENTIFICATION_ALGORITHM = "exact-release-track-consensus/2"
ALBUM_IDENTIFICATION_CONTEXT_SCHEMA = "groove-serpent.album-identification-context/2"

MAX_ALBUM_TRACKS = 2_048
MAX_EVIDENCE_TRACKS = 2_048
MAX_MATCHES_PER_TRACK = 20
MAX_RELEASES_PER_MATCH = 64
MAX_MANUAL_CANDIDATES = 64
MAX_TOTAL_MATCHES = 8_192
MAX_TOTAL_RELEASE_REFERENCES = 32_768
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_TEXT_LENGTH = 1_024
MAX_SHORT_TEXT_LENGTH = 128

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_RECOGNITION_RELEASE_KEYS = frozenset(
    {
        "release_mbid",
        "title",
        "release_group_mbid",
        "country",
        "date",
        "status",
        "release_group_title",
        "release_group_type",
        "release_group_secondary_types",
    }
)
_PRESSING_FIELDS = (
    "country",
    "date",
    "label",
    "catalog_number",
    "barcode",
    "media_formats",
    "matrix_runout",
)


def _strict_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ProjectValidationError(
            f"{label} fields are invalid (missing={missing}, extra={extra})."
        )


def _object(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ProjectValidationError(f"{label} must be a JSON object.")
    return cast(dict[str, Any], value)


def _array(value: Any, label: str) -> list[Any]:
    if type(value) is not list:
        raise ProjectValidationError(f"{label} must be a JSON array.")
    return value


def _text(
    value: Any,
    label: str,
    *,
    maximum: int = MAX_TEXT_LENGTH,
    allow_empty: bool = False,
) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or (not allow_empty and not value)
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        qualifier = "possibly-empty " if allow_empty else "nonempty "
        raise ProjectValidationError(
            f"{label} must be bounded, trimmed, {qualifier}printable text."
        )
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ProjectValidationError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProjectValidationError(
            f"{label} must be a JSON integer between {minimum} and {maximum}."
        )
    return value


def _score(value: Any, label: str) -> float:
    result = strict_finite_number(value, label)
    if not 0.0 <= result <= 1.0:
        raise ProjectValidationError(f"{label} must be between 0 and 1.")
    return result


def _uuid(value: Any, label: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProjectValidationError(f"{label} must be a canonical UUID.")
    try:
        normalized = str(uuid.UUID(value))
    except (AttributeError, ValueError) as exc:
        raise ProjectValidationError(f"{label} must be a canonical UUID.") from exc
    if normalized != value:
        raise ProjectValidationError(f"{label} must be a canonical lowercase UUID.")
    return normalized


def _optional_text(value: Any, label: str, maximum: int = MAX_TEXT_LENGTH) -> str:
    return _text(value, label, maximum=maximum, allow_empty=True)


def _release_date(value: Any, label: str) -> str:
    result = _optional_text(value, label, MAX_SHORT_TEXT_LENGTH)
    if not result:
        return result
    if _DATE_RE.fullmatch(result) is None:
        raise ProjectValidationError(
            f"{label} must be YYYY, YYYY-MM, or YYYY-MM-DD when present."
        )
    parts = [int(part) for part in result.split("-")]
    try:
        if len(parts) == 1:
            calendar_date(parts[0], 1, 1)
        elif len(parts) == 2:
            calendar_date(parts[0], parts[1], 1)
        else:
            calendar_date(parts[0], parts[1], parts[2])
    except ValueError as exc:
        raise ProjectValidationError(f"{label} is not a valid calendar date.") from exc
    return result


def _string_tuple(
    value: Any,
    label: str,
    *,
    maximum_items: int = 32,
    maximum_text: int = MAX_SHORT_TEXT_LENGTH,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum_items:
        raise ProjectValidationError(
            f"{label} must be an array with at most {maximum_items} entries."
        )
    result = tuple(
        _text(item, f"{label} item", maximum=maximum_text) for item in value
    )
    if len(set(result)) != len(result):
        raise ProjectValidationError(f"{label} must not contain duplicate entries.")
    return result


def _canonical_track_sha256(track: Track) -> str:
    return canonical_json_sha256(asdict(track))


def _track_ranges_sha256(tracks: Sequence[Track]) -> str:
    return canonical_json_sha256(
        [
            {
                "number": track.number,
                "start_sample": track.start_sample,
                "end_sample": track.end_sample,
                "track_sha256": _canonical_track_sha256(track),
            }
            for track in tracks
        ]
    )


@dataclass(frozen=True, slots=True)
class ReleaseCandidateFacts:
    """Strict release facts carried by one acoustic-recognition match."""

    release_mbid: str
    title: str
    release_group_mbid: str | None = None
    country: str = ""
    date: str = ""
    status: str = ""
    release_group_title: str = ""
    release_group_type: str = ""
    release_group_secondary_types: tuple[str, ...] = ()

    def validate(self) -> None:
        _uuid(self.release_mbid, "Release MusicBrainz ID")
        _text(self.title, "Release title")
        _uuid(
            self.release_group_mbid,
            "Release-group MusicBrainz ID",
            allow_none=True,
        )
        _optional_text(self.country, "Release country", MAX_SHORT_TEXT_LENGTH)
        if self.country and _COUNTRY_RE.fullmatch(self.country) is None:
            raise ProjectValidationError(
                "Release country must be an uppercase two-letter code when present."
            )
        _release_date(self.date, "Release date")
        for value, label in (
            (self.status, "Release status"),
            (self.release_group_title, "Release-group title"),
            (self.release_group_type, "Release-group type"),
        ):
            _optional_text(value, label, MAX_SHORT_TEXT_LENGTH)
        _string_tuple(
            self.release_group_secondary_types,
            "Release-group secondary types",
        )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "release_mbid": self.release_mbid,
            "title": self.title,
            "release_group_mbid": self.release_group_mbid,
            "country": self.country,
            "date": self.date,
            "status": self.status,
            "release_group_title": self.release_group_title,
            "release_group_type": self.release_group_type,
            "release_group_secondary_types": list(
                self.release_group_secondary_types
            ),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ReleaseCandidateFacts":
        """Normalize the bounded release shape emitted by ``RecognitionMatch``."""

        if any(not isinstance(key, str) for key in value):
            raise ProjectValidationError("Recognition release fields must have text keys.")
        extra = set(value) - _RECOGNITION_RELEASE_KEYS
        if extra:
            raise ProjectValidationError(
                "Recognition release contains unsupported network-derived field(s): "
                + ", ".join(sorted(extra))
            )
        raw_release_mbid = value.get("release_mbid")
        if raw_release_mbid in (None, ""):
            raise ProjectValidationError(
                "A ranked release candidate requires an exact MusicBrainz release ID."
            )
        secondary = value.get("release_group_secondary_types", [])
        candidate = cls(
            release_mbid=cast(str, raw_release_mbid),
            title=cast(str, value.get("title", "")),
            release_group_mbid=cast(str | None, value.get("release_group_mbid")),
            country=cast(str, value.get("country", "")),
            date=cast(str, value.get("date", "")),
            status=cast(str, value.get("status", "")),
            release_group_title=cast(str, value.get("release_group_title", "")),
            release_group_type=cast(str, value.get("release_group_type", "")),
            release_group_secondary_types=_string_tuple(
                secondary,
                "Release-group secondary types",
            ),
        )
        candidate.validate()
        return candidate

    @classmethod
    def from_dict(cls, value: Any) -> "ReleaseCandidateFacts":
        data = _object(value, "Release candidate")
        _strict_keys(data, set(cls.__dataclass_fields__), "Release candidate")
        candidate = cls(
            release_mbid=data["release_mbid"],
            title=data["title"],
            release_group_mbid=data["release_group_mbid"],
            country=data["country"],
            date=data["date"],
            status=data["status"],
            release_group_title=data["release_group_title"],
            release_group_type=data["release_group_type"],
            release_group_secondary_types=_string_tuple(
                data["release_group_secondary_types"],
                "Release-group secondary types",
            ),
        )
        candidate.validate()
        return candidate


@dataclass(frozen=True, slots=True)
class RecognitionObservation:
    """One strict, bounded recording match plus its candidate releases."""

    provider: str
    title: str
    artist_credit: str
    score: float
    recording_mbid: str | None
    release_group_ids: tuple[str, ...]
    releases: tuple[ReleaseCandidateFacts, ...]

    def validate(self) -> None:
        _text(self.provider, "Recognition provider", maximum=MAX_SHORT_TEXT_LENGTH)
        _text(self.title, "Recognized recording title")
        _optional_text(self.artist_credit, "Recognized artist credit")
        _score(self.score, "Recognition score")
        _uuid(self.recording_mbid, "Recording MusicBrainz ID", allow_none=True)
        if len(self.release_group_ids) > MAX_RELEASES_PER_MATCH:
            raise ProjectValidationError("Recognition release-group list is unbounded.")
        normalized_groups = tuple(
            cast(str, _uuid(item, "Release-group MusicBrainz ID"))
            for item in self.release_group_ids
        )
        if normalized_groups != self.release_group_ids:
            raise ProjectValidationError("Recognition release-group IDs are not canonical.")
        if len(set(self.release_group_ids)) != len(self.release_group_ids):
            raise ProjectValidationError("Recognition release-group IDs must be unique.")
        if len(self.releases) > MAX_RELEASES_PER_MATCH:
            raise ProjectValidationError(
                f"Recognition matches cannot exceed {MAX_RELEASES_PER_MATCH} releases."
            )
        seen: set[str] = set()
        for release in self.releases:
            if not isinstance(release, ReleaseCandidateFacts):
                raise ProjectValidationError(
                    "Recognition releases must use ReleaseCandidateFacts."
                )
            release.validate()
            if release.release_mbid in seen:
                raise ProjectValidationError(
                    "One recognition match cannot repeat a release candidate."
                )
            seen.add(release.release_mbid)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "provider": self.provider,
            "title": self.title,
            "artist_credit": self.artist_credit,
            "score": self.score,
            "recording_mbid": self.recording_mbid,
            "release_group_ids": list(self.release_group_ids),
            "releases": [release.to_dict() for release in self.releases],
        }

    @classmethod
    def from_recognition_match(cls, match: RecognitionMatch) -> "RecognitionObservation":
        if not isinstance(match, RecognitionMatch):
            raise ProjectValidationError("Recognition evidence must use RecognitionMatch values.")
        releases = tuple(
            ReleaseCandidateFacts.from_mapping(item) for item in match.release_candidates
        )
        observation = cls(
            provider=match.provider,
            title=match.title,
            artist_credit=match.artist_credit,
            score=match.score,
            recording_mbid=match.recording_mbid,
            release_group_ids=tuple(match.release_group_ids),
            releases=releases,
        )
        observation.validate()
        return observation

    @classmethod
    def from_dict(cls, value: Any) -> "RecognitionObservation":
        data = _object(value, "Recognition observation")
        _strict_keys(data, set(cls.__dataclass_fields__), "Recognition observation")
        groups = _array(data["release_group_ids"], "Recognition release-group IDs")
        releases = _array(data["releases"], "Recognition releases")
        observation = cls(
            provider=data["provider"],
            title=data["title"],
            artist_credit=data["artist_credit"],
            score=data["score"],
            recording_mbid=data["recording_mbid"],
            release_group_ids=tuple(groups),
            releases=tuple(ReleaseCandidateFacts.from_dict(item) for item in releases),
        )
        observation.validate()
        return observation


@dataclass(frozen=True, slots=True)
class TrackBinding:
    number: int
    start_sample: int
    end_sample: int
    track_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AlbumSideIdentificationContext:
    label: str
    order: int
    project_reference: str
    project_sha256: str
    project_revision: int
    project_state_sha256: str
    source_sha256: str
    source_size_bytes: int
    source_sample_rate: int
    speed_state_sha256: str
    requested_speed_factor: float
    fingerprint_asetrate_hz: int
    fingerprint_effective_speed_factor: float
    track_ranges_sha256: str
    tracks: tuple[TrackBinding, ...] = field(repr=False)

    def identity_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "order": self.order,
            "project_reference": self.project_reference,
            "project_sha256": self.project_sha256,
            "project_revision": self.project_revision,
            "project_state_sha256": self.project_state_sha256,
            "source_sha256": self.source_sha256,
            "source_size_bytes": self.source_size_bytes,
            "source_sample_rate": self.source_sample_rate,
            "speed_state_sha256": self.speed_state_sha256,
            "requested_speed_factor": self.requested_speed_factor,
            "fingerprint_asetrate_hz": self.fingerprint_asetrate_hz,
            "fingerprint_effective_speed_factor": (
                self.fingerprint_effective_speed_factor
            ),
            "fingerprint_speed_transform": RECOGNITION_SPEED_TRANSFORM,
            "track_count": len(self.tracks),
            "track_ranges_sha256": self.track_ranges_sha256,
            "tracks": [track.to_dict() for track in self.tracks],
        }

    def track(self, number: int) -> TrackBinding:
        for track in self.tracks:
            if track.number == number:
                return track
        raise ProjectValidationError(
            f"Side {self.label} has no current track numbered {number}."
        )


@dataclass(frozen=True, slots=True)
class AlbumIdentificationContext:
    album_reference: str
    album_sha256: str
    album_revision: int
    sides: tuple[AlbumSideIdentificationContext, ...]

    def identity_dict(self) -> dict[str, Any]:
        return {
            "album_reference": self.album_reference,
            "album_sha256": self.album_sha256,
            "album_revision": self.album_revision,
            "context_sha256": self.sha256,
            "side_count": len(self.sides),
            "track_count": sum(len(side.tracks) for side in self.sides),
            "sides": [side.identity_dict() for side in self.sides],
        }

    @property
    def sha256(self) -> str:
        payload = {
            "schema": ALBUM_IDENTIFICATION_CONTEXT_SCHEMA,
            "album_reference": self.album_reference,
            "album_sha256": self.album_sha256,
            "album_revision": self.album_revision,
            "sides": [side.identity_dict() for side in self.sides],
        }
        return canonical_json_sha256(payload)

    def side(self, label: str) -> AlbumSideIdentificationContext:
        matches = [side for side in self.sides if side.label == label]
        if len(matches) != 1:
            raise ProjectValidationError(
                f"Album identification requires one exact side label {label!r}."
            )
        return matches[0]

    def bind_track(
        self,
        side_label: str,
        track_number: int,
        matches: Sequence[RecognitionMatch],
    ) -> "TrackRecognitionEvidence":
        side = self.side(side_label)
        track = side.track(track_number)
        observations = tuple(
            RecognitionObservation.from_recognition_match(match) for match in matches
        )
        evidence = TrackRecognitionEvidence(
            schema=ALBUM_IDENTIFICATION_EVIDENCE_SCHEMA,
            context_sha256=self.sha256,
            album_sha256=self.album_sha256,
            side_label=side.label,
            side_order=side.order,
            project_sha256=side.project_sha256,
            project_revision=side.project_revision,
            project_state_sha256=side.project_state_sha256,
            source_sha256=side.source_sha256,
            source_sample_rate=side.source_sample_rate,
            speed_state_sha256=side.speed_state_sha256,
            requested_speed_factor=side.requested_speed_factor,
            fingerprint_asetrate_hz=side.fingerprint_asetrate_hz,
            fingerprint_effective_speed_factor=(
                side.fingerprint_effective_speed_factor
            ),
            fingerprint_speed_transform=RECOGNITION_SPEED_TRANSFORM,
            track_number=track.number,
            start_sample=track.start_sample,
            end_sample=track.end_sample,
            track_sha256=track.track_sha256,
            observations=observations,
        )
        evidence.validate()
        return evidence


@dataclass(frozen=True, slots=True)
class TrackRecognitionEvidence:
    """Recognition output bound to one exact track in one exact album context."""

    schema: str
    context_sha256: str
    album_sha256: str
    side_label: str
    side_order: int
    project_sha256: str
    project_revision: int
    project_state_sha256: str
    source_sha256: str
    source_sample_rate: int
    speed_state_sha256: str
    requested_speed_factor: float
    fingerprint_asetrate_hz: int
    fingerprint_effective_speed_factor: float
    fingerprint_speed_transform: str
    track_number: int
    start_sample: int
    end_sample: int
    track_sha256: str
    observations: tuple[RecognitionObservation, ...]

    def validate(self) -> None:
        if self.schema != ALBUM_IDENTIFICATION_EVIDENCE_SCHEMA:
            raise ProjectValidationError(
                f"Identification evidence schema must be "
                f"{ALBUM_IDENTIFICATION_EVIDENCE_SCHEMA!r}."
            )
        for value, label in (
            (self.context_sha256, "Identification context SHA-256"),
            (self.album_sha256, "Identification album SHA-256"),
            (self.project_sha256, "Identification project SHA-256"),
            (self.project_state_sha256, "Identification project-state SHA-256"),
            (self.source_sha256, "Identification source SHA-256"),
            (self.speed_state_sha256, "Identification speed-state SHA-256"),
            (self.track_sha256, "Identification track SHA-256"),
        ):
            _digest(value, label)
        _text(self.side_label, "Identification side label", maximum=32)
        _integer(self.side_order, "Identification side order", 1, 999)
        _integer(self.project_revision, "Identification project revision", 1, (1 << 63) - 1)
        _integer(self.track_number, "Identification track number", 1, MAX_TRACKS)
        _integer(
            self.source_sample_rate,
            "Identification source sample rate",
            1,
            768_000,
        )
        requested_factor = strict_finite_number(
            self.requested_speed_factor,
            "Identification requested speed factor",
        )
        effective_factor = strict_finite_number(
            self.fingerprint_effective_speed_factor,
            "Identification fingerprint effective speed factor",
        )
        expected_rate, expected_factor = speed_correction_details(
            self.source_sample_rate,
            requested_factor,
        )
        if self.fingerprint_asetrate_hz != expected_rate:
            raise ProjectValidationError(
                "Identification fingerprint asetrate is inconsistent."
            )
        if not math.isclose(
            effective_factor,
            expected_factor,
            rel_tol=0.0,
            abs_tol=1e-15,
        ):
            raise ProjectValidationError(
                "Identification fingerprint effective speed factor is inconsistent."
            )
        if self.fingerprint_speed_transform != RECOGNITION_SPEED_TRANSFORM:
            raise ProjectValidationError(
                "Identification fingerprint speed transform is unsupported."
            )
        _integer(self.start_sample, "Identification track start", 0, (1 << 63) - 1)
        _integer(self.end_sample, "Identification track end", 1, (1 << 63) - 1)
        if self.end_sample <= self.start_sample:
            raise ProjectValidationError("Identification track range must be positive.")
        if not 1 <= len(self.observations) <= MAX_MATCHES_PER_TRACK:
            raise ProjectValidationError(
                f"Track evidence must contain 1-{MAX_MATCHES_PER_TRACK} matches."
            )
        for observation in self.observations:
            if not isinstance(observation, RecognitionObservation):
                raise ProjectValidationError(
                    "Track evidence observations must use RecognitionObservation."
                )
            observation.validate()

    @property
    def sha256(self) -> str:
        return canonical_json_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload = asdict(self)
        payload["observations"] = [item.to_dict() for item in self.observations]
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> "TrackRecognitionEvidence":
        data = _object(value, "Track recognition evidence")
        _strict_keys(data, set(cls.__dataclass_fields__), "Track recognition evidence")
        raw_observations = _array(data["observations"], "Track recognition observations")
        evidence = cls(
            schema=data["schema"],
            context_sha256=data["context_sha256"],
            album_sha256=data["album_sha256"],
            side_label=data["side_label"],
            side_order=data["side_order"],
            project_sha256=data["project_sha256"],
            project_revision=data["project_revision"],
            project_state_sha256=data["project_state_sha256"],
            source_sha256=data["source_sha256"],
            source_sample_rate=data["source_sample_rate"],
            speed_state_sha256=data["speed_state_sha256"],
            requested_speed_factor=data["requested_speed_factor"],
            fingerprint_asetrate_hz=data["fingerprint_asetrate_hz"],
            fingerprint_effective_speed_factor=data[
                "fingerprint_effective_speed_factor"
            ],
            fingerprint_speed_transform=data["fingerprint_speed_transform"],
            track_number=data["track_number"],
            start_sample=data["start_sample"],
            end_sample=data["end_sample"],
            track_sha256=data["track_sha256"],
            observations=tuple(
                RecognitionObservation.from_dict(item) for item in raw_observations
            ),
        )
        evidence.validate()
        return evidence


@dataclass(frozen=True, slots=True)
class ManualReleaseCandidate:
    """Unranked owner-review input; never automatic identification evidence."""

    title: str
    source_description: str
    release_mbid: str | None = None
    release_group_mbid: str | None = None
    artist_credit: str = ""
    country: str = ""
    date: str = ""
    status: str = ""
    label: str = ""
    catalog_number: str = ""
    barcode: str = ""
    media_formats: tuple[str, ...] = ()
    track_count: int | None = None
    matrix_runout: str = ""
    note: str = ""

    def validate(self) -> None:
        _text(self.title, "Manual release title")
        _text(self.source_description, "Manual candidate source description")
        _uuid(self.release_mbid, "Manual release MusicBrainz ID", allow_none=True)
        _uuid(
            self.release_group_mbid,
            "Manual release-group MusicBrainz ID",
            allow_none=True,
        )
        for value, label, maximum in (
            (self.artist_credit, "Manual artist credit", MAX_TEXT_LENGTH),
            (self.country, "Manual country", MAX_SHORT_TEXT_LENGTH),
            (self.date, "Manual date", MAX_SHORT_TEXT_LENGTH),
            (self.status, "Manual status", MAX_SHORT_TEXT_LENGTH),
            (self.label, "Manual label", MAX_TEXT_LENGTH),
            (self.catalog_number, "Manual catalog number", MAX_SHORT_TEXT_LENGTH),
            (self.barcode, "Manual barcode", MAX_SHORT_TEXT_LENGTH),
            (self.matrix_runout, "Manual matrix/runout", MAX_TEXT_LENGTH),
            (self.note, "Manual candidate note", MAX_TEXT_LENGTH),
        ):
            _optional_text(value, label, maximum)
        if self.country and _COUNTRY_RE.fullmatch(self.country) is None:
            raise ProjectValidationError(
                "Manual country must be an uppercase two-letter code when present."
            )
        _release_date(self.date, "Manual date")
        _string_tuple(self.media_formats, "Manual media formats")
        if self.track_count is not None:
            _integer(self.track_count, "Manual track count", 1, MAX_ALBUM_TRACKS)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload = asdict(self)
        payload["media_formats"] = list(self.media_formats)
        payload["ranking_authority"] = "none-review-input-only"
        return payload

    @classmethod
    def from_output_dict(cls, value: Any) -> "ManualReleaseCandidate":
        data = _object(value, "Manual release candidate")
        expected = set(cls.__dataclass_fields__) | {"ranking_authority"}
        _strict_keys(data, expected, "Manual release candidate")
        if data["ranking_authority"] != "none-review-input-only":
            raise ProjectValidationError("Manual candidate grants ranking authority.")
        candidate = cls(
            title=data["title"],
            source_description=data["source_description"],
            release_mbid=data["release_mbid"],
            release_group_mbid=data["release_group_mbid"],
            artist_credit=data["artist_credit"],
            country=data["country"],
            date=data["date"],
            status=data["status"],
            label=data["label"],
            catalog_number=data["catalog_number"],
            barcode=data["barcode"],
            media_formats=_string_tuple(
                data["media_formats"],
                "Manual media formats",
            ),
            track_count=data["track_count"],
            matrix_runout=data["matrix_runout"],
            note=data["note"],
        )
        candidate.validate()
        return candidate


@dataclass(frozen=True, slots=True)
class AlbumIdentificationConfig:
    minimum_supporting_tracks: int = 2
    minimum_track_coverage: float = 0.50
    minimum_mean_score: float = 0.75
    minimum_rank_margin: float = 0.10
    high_confidence_track_coverage: float = 0.75
    high_confidence_mean_score: float = 0.90
    high_confidence_rank_margin: float = 0.15

    def validate(self) -> None:
        _integer(
            self.minimum_supporting_tracks,
            "Minimum supporting tracks",
            2,
            MAX_ALBUM_TRACKS,
        )
        values = {
            "minimum track coverage": self.minimum_track_coverage,
            "minimum mean score": self.minimum_mean_score,
            "minimum rank margin": self.minimum_rank_margin,
            "high-confidence track coverage": self.high_confidence_track_coverage,
            "high-confidence mean score": self.high_confidence_mean_score,
            "high-confidence rank margin": self.high_confidence_rank_margin,
        }
        normalized = {label: _score(value, label) for label, value in values.items()}
        if normalized["high-confidence track coverage"] < normalized[
            "minimum track coverage"
        ]:
            raise ProjectValidationError(
                "High-confidence coverage cannot be below minimum coverage."
            )
        if normalized["high-confidence mean score"] < normalized[
            "minimum mean score"
        ]:
            raise ProjectValidationError(
                "High-confidence mean score cannot be below its minimum."
            )
        if normalized["high-confidence rank margin"] < normalized[
            "minimum rank margin"
        ]:
            raise ProjectValidationError(
                "High-confidence rank margin cannot be below its minimum."
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass(slots=True)
class _CandidateAggregate:
    facts: list[ReleaseCandidateFacts] = field(default_factory=list)
    supports: dict[tuple[int, int], dict[str, Any]] = field(default_factory=dict)


def _translate_export_error(label: str, exc: ExportError) -> ProjectValidationError:
    return ProjectValidationError(f"{label} could not be identity-verified: {exc}")


def _check_pin(
    album: AlbumProject,
    side_index: int,
    project: Project,
    project_sha256: str,
    source_receipt: FileReceipt,
) -> None:
    side = album.sides[side_index]
    if side.pin is None:
        raise ProjectValidationError(
            f"Side {side.label} must be explicitly pinned before identification."
        )
    current_speed = project_speed_state(project)
    comparisons: tuple[tuple[object, object, str], ...] = (
        (side.pin.project_revision, project.revision, "project revision"),
        (side.pin.project_sha256, project_sha256, "project file"),
        (side.pin.editable_state_sha256, project.state_sha256, "editable state"),
        (side.pin.source_sha256, source_receipt.sha256, "source audio"),
        (
            side.pin.project_speed_state_sha256,
            current_speed.sha256,
            "project speed state",
        ),
        (
            side.pin.speed_state_sha256,
            side.speed.state_sha256,
            "selected speed state",
        ),
    )
    changed = [label for expected, current, label in comparisons if expected != current]
    if changed:
        raise ProjectValidationError(
            f"Side {side.label} identification context is stale: {', '.join(changed)}."
        )
    if not project.source.sha256 or project.source.sha256.lower() != source_receipt.sha256:
        raise ProjectValidationError(
            f"Side {side.label} source does not match its project SHA-256."
        )
    if project.source.size_bytes != source_receipt.size_bytes:
        raise ProjectValidationError(
            f"Side {side.label} source does not match its project byte length."
        )


def capture_album_identification_context(album_path: Path) -> AlbumIdentificationContext:
    """Capture and verify the exact read-only album context for recognition evidence."""

    album_path = album_path.expanduser()
    album, album_sha256 = load_album_project_with_sha256(album_path)
    if len(album.sides) > 64:
        raise ProjectValidationError("Album identification side count is unbounded.")

    source_receipts: dict[Path, FileReceipt] = {}
    project_receipts: list[tuple[Path, str]] = []
    side_contexts: list[AlbumSideIdentificationContext] = []
    total_tracks = 0
    for index, side in enumerate(album.sides):
        project_path = resolve_album_reference(
            album_path,
            side.project,
            f"Side {side.label} project",
        )
        project, project_sha256 = load_project_with_sha256(project_path)
        project_receipts.append((project_path, project_sha256))
        source_path = resolve_source_path(project, project_path).resolve()
        receipt = source_receipts.get(source_path)
        if receipt is None:
            try:
                receipt = capture_file_receipt(
                    source_path,
                    label=f"Side {side.label} source audio",
                )
            except ExportError as exc:
                raise _translate_export_error(
                    f"Side {side.label} source audio",
                    exc,
                ) from exc
            source_receipts[source_path] = receipt
        _check_pin(album, index, project, project_sha256, receipt)

        total_tracks += len(project.tracks)
        if total_tracks > MAX_ALBUM_TRACKS:
            raise ProjectValidationError(
                f"Album identification supports at most {MAX_ALBUM_TRACKS} tracks."
            )
        bindings = tuple(
            TrackBinding(
                number=track.number,
                start_sample=track.start_sample,
                end_sample=track.end_sample,
                track_sha256=_canonical_track_sha256(track),
            )
            for track in project.tracks
        )
        fingerprint_asetrate_hz, fingerprint_effective_speed_factor = (
            speed_correction_details(
                project.source.sample_rate,
                side.effective_speed_factor,
            )
        )
        side_contexts.append(
            # Recognition corrects both pitch and tempo using the same integer
            # asetrate geometry as publication.  The source snapshot remains
            # immutable; this identity describes only the transient pipeline.
            AlbumSideIdentificationContext(
                label=side.label,
                order=side.order,
                project_reference=side.project,
                project_sha256=project_sha256,
                project_revision=project.revision,
                project_state_sha256=project.state_sha256,
                source_sha256=receipt.sha256,
                source_size_bytes=receipt.size_bytes,
                source_sample_rate=project.source.sample_rate,
                speed_state_sha256=side.speed.state.sha256,
                requested_speed_factor=side.effective_speed_factor,
                fingerprint_asetrate_hz=fingerprint_asetrate_hz,
                fingerprint_effective_speed_factor=(
                    fingerprint_effective_speed_factor
                ),
                track_ranges_sha256=_track_ranges_sha256(project.tracks),
                tracks=bindings,
            )
        )

    repeated_album, repeated_album_sha256 = load_album_project_with_sha256(album_path)
    if repeated_album_sha256 != album_sha256 or repeated_album.revision != album.revision:
        raise ProjectValidationError(
            "Album project changed during identification context capture."
        )
    for project_path, expected_sha256 in project_receipts:
        _project, repeated_sha256 = load_project_with_sha256(project_path)
        if repeated_sha256 != expected_sha256:
            raise ProjectValidationError(
                "A side project changed during identification context capture."
            )
    for source_path, receipt in source_receipts.items():
        try:
            assert_file_receipt(source_path, receipt, label="Album source audio")
        except ExportError as exc:
            raise _translate_export_error("Album source audio", exc) from exc

    return AlbumIdentificationContext(
        album_reference=album_path.name,
        album_sha256=album_sha256,
        album_revision=album.revision,
        sides=tuple(side_contexts),
    )


def _validate_evidence_binding(
    context: AlbumIdentificationContext,
    evidence: TrackRecognitionEvidence,
) -> tuple[int, int]:
    evidence.validate()
    if evidence.context_sha256 != context.sha256:
        raise ProjectValidationError("Track recognition evidence context is stale.")
    if evidence.album_sha256 != context.album_sha256:
        raise ProjectValidationError("Track recognition evidence album is stale.")
    side = context.side(evidence.side_label)
    expected_side = {
        "side_order": side.order,
        "project_sha256": side.project_sha256,
        "project_revision": side.project_revision,
        "project_state_sha256": side.project_state_sha256,
        "source_sha256": side.source_sha256,
        "source_sample_rate": side.source_sample_rate,
        "speed_state_sha256": side.speed_state_sha256,
        "requested_speed_factor": side.requested_speed_factor,
        "fingerprint_asetrate_hz": side.fingerprint_asetrate_hz,
        "fingerprint_effective_speed_factor": (
            side.fingerprint_effective_speed_factor
        ),
        "fingerprint_speed_transform": RECOGNITION_SPEED_TRANSFORM,
    }
    for field_name, expected in expected_side.items():
        if getattr(evidence, field_name) != expected:
            raise ProjectValidationError(
                f"Track recognition evidence has a stale {field_name.replace('_', ' ')}."
            )
    track = side.track(evidence.track_number)
    if (
        evidence.start_sample,
        evidence.end_sample,
        evidence.track_sha256,
    ) != (track.start_sample, track.end_sample, track.track_sha256):
        raise ProjectValidationError("Track recognition evidence range or track state is stale.")
    return side.order, track.number


def _merge_release_facts(
    release_mbid: str,
    facts: Sequence[ReleaseCandidateFacts],
) -> tuple[dict[str, Any] | None, list[str]]:
    fields = (
        "title",
        "release_group_mbid",
        "country",
        "date",
        "status",
        "release_group_title",
        "release_group_type",
        "release_group_secondary_types",
    )
    merged: dict[str, Any] = {"release_mbid": release_mbid}
    conflicts: list[str] = []
    for field_name in fields:
        values = {
            getattr(item, field_name)
            for item in facts
            if getattr(item, field_name) not in (None, "", ())
        }
        if len(values) > 1:
            conflicts.append(field_name)
        elif values:
            merged[field_name] = next(iter(values))
        else:
            merged[field_name] = None if field_name == "release_group_mbid" else (
                [] if field_name == "release_group_secondary_types" else ""
            )
    if conflicts:
        return None, conflicts
    secondary = merged["release_group_secondary_types"]
    if isinstance(secondary, tuple):
        merged["release_group_secondary_types"] = list(secondary)
    return merged, []


def _canonical_payload_bytes(value: Mapping[str, Any] | list[Any]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProjectValidationError(
            f"Identification evidence is not finite canonical JSON: {exc}"
        ) from exc


def _bounded_evidence_payload(
    ordered_evidence: Sequence[tuple[tuple[int, int], TrackRecognitionEvidence]],
) -> tuple[list[dict[str, Any]], str, dict[tuple[int, int], str]]:
    total_matches = sum(len(item.observations) for _key, item in ordered_evidence)
    total_releases = sum(
        len(observation.releases)
        for _key, item in ordered_evidence
        for observation in item.observations
    )
    if total_matches > MAX_TOTAL_MATCHES:
        raise ProjectValidationError(
            f"Identification evidence cannot exceed {MAX_TOTAL_MATCHES} total matches."
        )
    if total_releases > MAX_TOTAL_RELEASE_REFERENCES:
        raise ProjectValidationError(
            "Identification evidence cannot exceed "
            f"{MAX_TOTAL_RELEASE_REFERENCES} total release references."
        )
    documents = [item.to_dict() for _key, item in ordered_evidence]
    raw = _canonical_payload_bytes(documents)
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise ProjectValidationError(
            f"Identification evidence exceeds the {MAX_EVIDENCE_BYTES}-byte limit."
        )
    item_hashes = {
        key: hashlib.sha256(_canonical_payload_bytes(document)).hexdigest()
        for (key, _item), document in zip(ordered_evidence, documents, strict=True)
    }
    return documents, hashlib.sha256(raw).hexdigest(), item_hashes


def _aggregate_evidence(
    ordered_evidence: Sequence[tuple[tuple[int, int], TrackRecognitionEvidence]],
    item_hashes: Mapping[tuple[int, int], str],
) -> dict[str, _CandidateAggregate]:
    aggregates: dict[str, _CandidateAggregate] = {}
    for (side_order, track_number), item in ordered_evidence:
        track_best: dict[
            str,
            tuple[RecognitionObservation, ReleaseCandidateFacts],
        ] = {}
        for observation in item.observations:
            for release in observation.releases:
                previous = track_best.get(release.release_mbid)
                if previous is None or observation.score > previous[0].score:
                    track_best[release.release_mbid] = (observation, release)
                aggregate = aggregates.setdefault(
                    release.release_mbid,
                    _CandidateAggregate(),
                )
                aggregate.facts.append(release)
        for release_mbid, (observation, _release) in sorted(track_best.items()):
            aggregate = aggregates[release_mbid]
            aggregate.supports[(side_order, track_number)] = {
                "side_label": item.side_label,
                "side_order": side_order,
                "track_number": track_number,
                "score": round(observation.score, 9),
                "provider": observation.provider,
                "recording_mbid": observation.recording_mbid,
                "recording_title": observation.title,
                "artist_credit": observation.artist_credit,
                "evidence_sha256": item_hashes[(side_order, track_number)],
            }
    return aggregates


def _rank_candidates(
    aggregates: Mapping[str, _CandidateAggregate],
    *,
    total_tracks: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for release_mbid, aggregate in sorted(aggregates.items()):
        merged, conflicting_fields = _merge_release_facts(
            release_mbid,
            aggregate.facts,
        )
        if merged is None:
            conflicts.append(
                {
                    "release_mbid": release_mbid,
                    "conflicting_fields": conflicting_fields,
                    "disposition": "excluded-from-ranking",
                }
            )
            continue
        support = [aggregate.supports[key] for key in sorted(aggregate.supports)]
        scores = [cast(float, item["score"]) for item in support]
        side_orders = {cast(int, item["side_order"]) for item in support}
        score_sum = math.fsum(scores)
        support_count = len(support)
        mean_score = score_sum / support_count
        coverage = support_count / total_tracks
        candidates.append(
            {
                "rank": 0,
                "release": merged,
                "release_mbid": release_mbid,
                "evidence_score": round(score_sum / total_tracks, 9),
                "mean_recognition_score": round(mean_score, 9),
                "supporting_track_count": support_count,
                "supporting_side_count": len(side_orders),
                "album_track_coverage": round(coverage, 9),
                "support": support,
                "pressing_identity_status": "candidate-not-proven",
            }
        )
    candidates.sort(
        key=lambda item: (
            -cast(float, item["evidence_score"]),
            -cast(int, item["supporting_track_count"]),
            -cast(int, item["supporting_side_count"]),
            cast(str, item["release_mbid"]),
        )
    )
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
    return candidates, conflicts


def _decision(
    candidates: Sequence[dict[str, Any]],
    *,
    total_sides: int,
    config: AlbumIdentificationConfig,
    has_conflicting_release_facts: bool = False,
) -> dict[str, Any]:
    if has_conflicting_release_facts:
        conflict_margin: float | None = None
        if len(candidates) > 1:
            conflict_margin = round(
                cast(float, candidates[0]["evidence_score"])
                - cast(float, candidates[1]["evidence_score"]),
                9,
            )
        return {
            "status": "abstained",
            "confidence": "none",
            "selected_release_mbid": None,
            "rank_margin": conflict_margin,
            "reasons": ["conflicting_network_release_facts_require_review"],
        }
    if not candidates:
        return {
            "status": "abstained",
            "confidence": "none",
            "selected_release_mbid": None,
            "rank_margin": None,
            "reasons": ["no_consistent_exact_release_candidates"],
        }
    top = candidates[0]
    reasons: list[str] = []
    if cast(int, top["supporting_track_count"]) < config.minimum_supporting_tracks:
        reasons.append("insufficient_independent_track_support")
    if cast(float, top["album_track_coverage"]) < config.minimum_track_coverage:
        reasons.append("insufficient_album_track_coverage")
    if cast(float, top["mean_recognition_score"]) < config.minimum_mean_score:
        reasons.append("recognition_scores_too_low")
    required_sides = total_sides
    if cast(int, top["supporting_side_count"]) < required_sides:
        reasons.append("candidate_not_supported_across_independent_sides")

    margin: float | None = None
    if len(candidates) > 1:
        margin = round(
            cast(float, top["evidence_score"])
            - cast(float, candidates[1]["evidence_score"]),
            9,
        )
    if not reasons and margin is not None and margin < config.minimum_rank_margin:
        return {
            "status": "ambiguous",
            "confidence": "low",
            "selected_release_mbid": None,
            "rank_margin": margin,
            "reasons": ["top_release_candidates_are_not_separated"],
        }
    if reasons:
        return {
            "status": "abstained",
            "confidence": "none",
            "selected_release_mbid": None,
            "rank_margin": margin,
            "reasons": reasons,
        }
    high_margin = margin is None or margin >= config.high_confidence_rank_margin
    confidence = (
        "high"
        if cast(float, top["album_track_coverage"])
        >= config.high_confidence_track_coverage
        and cast(float, top["mean_recognition_score"])
        >= config.high_confidence_mean_score
        and cast(int, top["supporting_side_count"]) == total_sides
        and high_margin
        else "medium"
    )
    return {
        "status": "proposed",
        "confidence": confidence,
        "selected_release_mbid": top["release_mbid"],
        "rank_margin": margin,
        "reasons": ["cross_track_release_consensus_requires_owner_review"],
    }


def _pressing_review(
    candidates: Sequence[dict[str, Any]],
    manual_candidates: Sequence[ManualReleaseCandidate],
) -> dict[str, Any]:
    top_release = candidates[0]["release"] if candidates else None
    known: dict[str, Any] = {}
    if isinstance(top_release, dict):
        for field_name in (
            "release_mbid",
            "title",
            "release_group_mbid",
            "status",
            "country",
            "date",
        ):
            known[field_name] = top_release.get(field_name, "")
    else:
        known = {
            "release_mbid": "",
            "title": "",
            "release_group_mbid": "",
            "status": "",
            "country": "",
            "date": "",
        }
    for field_name in _PRESSING_FIELDS:
        known.setdefault(field_name, [] if field_name == "media_formats" else "")
    missing = [field_name for field_name in _PRESSING_FIELDS if not known[field_name]]
    return {
        "proof_status": "not-proven",
        "database_release_id_is_physical_pressing_proof": False,
        "top_ranked_known_facts": known,
        "missing_or_unverified_facts": missing,
        "owner_checks_required": [
            "compare label and catalog number",
            "compare country, date, barcode, and media format",
            "inspect matrix/runout inscriptions",
            "visually compare the physical sleeve and labels with candidate artwork",
        ],
        "manual_candidates": [item.to_dict() for item in manual_candidates],
        "manual_candidates_affect_automatic_ranking": False,
    }


def propose_album_release_identification(
    album_path: Path,
    evidence: Sequence[TrackRecognitionEvidence],
    *,
    manual_candidates: Sequence[ManualReleaseCandidate] = (),
    config: AlbumIdentificationConfig | None = None,
) -> dict[str, Any]:
    """Return a deterministic, proposal-only ranked release consensus."""

    chosen_config = config or AlbumIdentificationConfig()
    chosen_config.validate()
    if not 1 <= len(evidence) <= MAX_EVIDENCE_TRACKS:
        raise ProjectValidationError(
            f"Album identification requires 1-{MAX_EVIDENCE_TRACKS} track observations."
        )
    if len(manual_candidates) > MAX_MANUAL_CANDIDATES:
        raise ProjectValidationError(
            f"Manual release candidates cannot exceed {MAX_MANUAL_CANDIDATES}."
        )
    for manual in manual_candidates:
        if not isinstance(manual, ManualReleaseCandidate):
            raise ProjectValidationError(
                "Manual candidates must use ManualReleaseCandidate."
            )
        manual.validate()

    context = capture_album_identification_context(album_path)
    ordered_evidence: list[tuple[tuple[int, int], TrackRecognitionEvidence]] = []
    seen_tracks: set[tuple[int, int]] = set()
    for item in evidence:
        if not isinstance(item, TrackRecognitionEvidence):
            raise ProjectValidationError(
                "Identification evidence must use TrackRecognitionEvidence."
            )
        key = _validate_evidence_binding(context, item)
        if key in seen_tracks:
            raise ProjectValidationError(
                "A track may contribute only one identification evidence document."
            )
        seen_tracks.add(key)
        ordered_evidence.append((key, item))
    ordered_evidence.sort(key=lambda entry: entry[0])

    evidence_payload, evidence_sha256, evidence_hashes = _bounded_evidence_payload(
        ordered_evidence
    )
    aggregates = _aggregate_evidence(ordered_evidence, evidence_hashes)

    total_tracks = sum(len(side.tracks) for side in context.sides)
    candidates, conflicts = _rank_candidates(aggregates, total_tracks=total_tracks)
    decision = _decision(
        candidates,
        total_sides=len(context.sides),
        config=chosen_config,
        has_conflicting_release_facts=bool(conflicts),
    )
    config_dict = chosen_config.to_dict()
    module_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    body: dict[str, Any] = {
        "schema": ALBUM_IDENTIFICATION_PROPOSAL_SCHEMA,
        "algorithm": {
            "id": ALBUM_IDENTIFICATION_ALGORITHM,
            "module": "groove_serpent.album_identification",
            "module_sha256": module_sha256,
            "app_version": __version__,
        },
        "album": context.identity_dict(),
        "evidence": {
            "schema": "groove-serpent.album-identification-evidence-set/1",
            "sha256": evidence_sha256,
            "observed_track_count": len(ordered_evidence),
            "album_track_count": total_tracks,
            "items": evidence_payload,
        },
        "config": {
            "values": config_dict,
            "sha256": canonical_json_sha256(config_dict),
        },
        "ranked_release_candidates": candidates,
        "excluded_conflicts": conflicts,
        "decision": decision,
        "exact_pressing_review": _pressing_review(candidates, manual_candidates),
        "authority": {
            "may_modify_album_project": False,
            "may_modify_side_projects": False,
            "may_apply_metadata": False,
            "may_download_or_apply_artwork": False,
            "may_change_topology_speed_or_restoration": False,
            "may_publish": False,
            "human_review_required": True,
            "physical_pressing_proven": False,
        },
    }
    proposal = dict(body)
    proposal["proposal_sha256"] = canonical_json_sha256(body)
    validate_album_identification_proposal(proposal)

    repeated_context = capture_album_identification_context(album_path)
    if repeated_context.sha256 != context.sha256:
        raise ProjectValidationError(
            "Album identification context changed while the proposal was built."
        )
    return proposal


def validate_album_identification_proposal(value: Any) -> None:
    """Validate proposal structure, digest, ranking, decision, and authority."""

    proposal = _object(value, "Album identification proposal")
    expected = {
        "schema",
        "algorithm",
        "album",
        "evidence",
        "config",
        "ranked_release_candidates",
        "excluded_conflicts",
        "decision",
        "exact_pressing_review",
        "authority",
        "proposal_sha256",
    }
    _strict_keys(proposal, expected, "Album identification proposal")
    if proposal["schema"] != ALBUM_IDENTIFICATION_PROPOSAL_SCHEMA:
        raise ProjectValidationError("Album identification proposal schema is unsupported.")
    body = {key: proposal[key] for key in expected if key != "proposal_sha256"}
    digest = _digest(proposal["proposal_sha256"], "Identification proposal SHA-256")
    if digest != canonical_json_sha256(body):
        raise ProjectValidationError("Album identification proposal SHA-256 is invalid.")

    algorithm = _object(proposal["algorithm"], "Identification algorithm")
    _strict_keys(
        algorithm,
        {"id", "module", "module_sha256", "app_version"},
        "Identification algorithm",
    )
    if algorithm["id"] != ALBUM_IDENTIFICATION_ALGORITHM:
        raise ProjectValidationError("Identification algorithm ID is unsupported.")
    if algorithm["module"] != "groove_serpent.album_identification":
        raise ProjectValidationError("Identification algorithm module is unsupported.")
    _digest(algorithm["module_sha256"], "Identification module SHA-256")
    _text(algorithm["app_version"], "Identification app version", maximum=128)

    album = _object(proposal["album"], "Identification album identity")
    album_keys = {
        "album_reference",
        "album_sha256",
        "album_revision",
        "context_sha256",
        "side_count",
        "track_count",
        "sides",
    }
    _strict_keys(album, album_keys, "Identification album identity")
    _text(album["album_reference"], "Identification album reference")
    _digest(album["album_sha256"], "Identification album SHA-256")
    _digest(album["context_sha256"], "Identification context SHA-256")
    _integer(album["album_revision"], "Identification album revision", 1, (1 << 63) - 1)
    side_count = _integer(album["side_count"], "Identification side count", 1, 64)
    track_count = _integer(
        album["track_count"],
        "Identification album track count",
        1,
        MAX_ALBUM_TRACKS,
    )
    sides = _array(album["sides"], "Identification album sides")
    if len(sides) != side_count:
        raise ProjectValidationError("Identification album side count is inconsistent.")
    side_keys = {
        "label",
        "order",
        "project_reference",
        "project_sha256",
        "project_revision",
        "project_state_sha256",
        "source_sha256",
        "source_size_bytes",
        "source_sample_rate",
        "speed_state_sha256",
        "requested_speed_factor",
        "fingerprint_asetrate_hz",
        "fingerprint_effective_speed_factor",
        "fingerprint_speed_transform",
        "track_count",
        "track_ranges_sha256",
        "tracks",
    }
    normalized_sides: list[dict[str, Any]] = []
    side_by_label: dict[str, dict[str, Any]] = {}
    counted_tracks = 0
    for expected_order, raw_side in enumerate(sides, start=1):
        side = _object(raw_side, f"Identification side {expected_order}")
        _strict_keys(side, side_keys, f"Identification side {expected_order}")
        label = _text(side["label"], "Identification side label", maximum=32)
        if label in side_by_label:
            raise ProjectValidationError("Identification side labels must be unique.")
        if side["order"] != expected_order:
            raise ProjectValidationError("Identification side order must be consecutive.")
        _text(side["project_reference"], "Identification project reference")
        for field_name, field_label in (
            ("project_sha256", "Identification project SHA-256"),
            ("project_state_sha256", "Identification project-state SHA-256"),
            ("source_sha256", "Identification source SHA-256"),
            ("speed_state_sha256", "Identification speed-state SHA-256"),
            ("track_ranges_sha256", "Identification track-ranges SHA-256"),
        ):
            _digest(side[field_name], field_label)
        _integer(
            side["project_revision"],
            "Identification project revision",
            1,
            (1 << 63) - 1,
        )
        _integer(
            side["source_size_bytes"],
            "Identification source byte length",
            0,
            (1 << 63) - 1,
        )
        source_sample_rate = _integer(
            side["source_sample_rate"],
            "Identification source sample rate",
            1,
            768_000,
        )
        requested_factor = strict_finite_number(
            side["requested_speed_factor"],
            "Identification requested speed factor",
        )
        effective_factor = strict_finite_number(
            side["fingerprint_effective_speed_factor"],
            "Identification fingerprint effective speed factor",
        )
        expected_asetrate, expected_effective_factor = speed_correction_details(
            source_sample_rate,
            requested_factor,
        )
        if side["fingerprint_asetrate_hz"] != expected_asetrate:
            raise ProjectValidationError(
                "Identification fingerprint asetrate is inconsistent."
            )
        if not math.isclose(
            effective_factor,
            expected_effective_factor,
            rel_tol=0.0,
            abs_tol=1e-15,
        ):
            raise ProjectValidationError(
                "Identification fingerprint effective speed factor is inconsistent."
            )
        if side["fingerprint_speed_transform"] != RECOGNITION_SPEED_TRANSFORM:
            raise ProjectValidationError(
                "Identification fingerprint speed transform is unsupported."
            )
        side_track_count = _integer(
            side["track_count"],
            "Identification side track count",
            1,
            MAX_TRACKS,
        )
        raw_tracks = _array(side["tracks"], "Identification side tracks")
        if len(raw_tracks) != side_track_count:
            raise ProjectValidationError("Identification side track count is inconsistent.")
        track_by_number: dict[int, dict[str, Any]] = {}
        for raw_track in raw_tracks:
            track = _object(raw_track, "Identification track binding")
            _strict_keys(
                track,
                {"number", "start_sample", "end_sample", "track_sha256"},
                "Identification track binding",
            )
            number = _integer(
                track["number"],
                "Identification track number",
                1,
                MAX_TRACKS,
            )
            start = _integer(
                track["start_sample"],
                "Identification track start",
                0,
                (1 << 63) - 1,
            )
            end = _integer(
                track["end_sample"],
                "Identification track end",
                1,
                (1 << 63) - 1,
            )
            if end <= start or number in track_by_number:
                raise ProjectValidationError(
                    "Identification track bindings must be unique positive ranges."
                )
            _digest(track["track_sha256"], "Identification track SHA-256")
            track_by_number[number] = track
        if canonical_json_sha256(raw_tracks) != side["track_ranges_sha256"]:
            raise ProjectValidationError("Identification track-ranges SHA-256 is invalid.")
        counted_tracks += side_track_count
        normalized_sides.append(side)
        side_by_label[label] = {**side, "track_by_number": track_by_number}
    if counted_tracks != track_count:
        raise ProjectValidationError("Identification album track count is inconsistent.")
    context_body = {
        "schema": ALBUM_IDENTIFICATION_CONTEXT_SCHEMA,
        "album_reference": album["album_reference"],
        "album_sha256": album["album_sha256"],
        "album_revision": album["album_revision"],
        "sides": normalized_sides,
    }
    if canonical_json_sha256(context_body) != album["context_sha256"]:
        raise ProjectValidationError("Identification context SHA-256 is invalid.")

    evidence_set = _object(proposal["evidence"], "Identification evidence set")
    _strict_keys(
        evidence_set,
        {"schema", "sha256", "observed_track_count", "album_track_count", "items"},
        "Identification evidence set",
    )
    if evidence_set["schema"] != "groove-serpent.album-identification-evidence-set/1":
        raise ProjectValidationError("Identification evidence-set schema is unsupported.")
    if evidence_set["album_track_count"] != track_count:
        raise ProjectValidationError("Identification evidence album count is inconsistent.")
    items = _array(evidence_set["items"], "Identification evidence items")
    observed_count = _integer(
        evidence_set["observed_track_count"],
        "Identification observed track count",
        1,
        MAX_EVIDENCE_TRACKS,
    )
    if observed_count != len(items):
        raise ProjectValidationError("Identification observed track count is inconsistent.")
    _digest(evidence_set["sha256"], "Identification evidence-set SHA-256")
    if hashlib.sha256(_canonical_payload_bytes(items)).hexdigest() != evidence_set["sha256"]:
        raise ProjectValidationError("Identification evidence-set SHA-256 is invalid.")

    ordered_evidence: list[tuple[tuple[int, int], TrackRecognitionEvidence]] = []
    seen_evidence: set[tuple[int, int]] = set()
    for raw_item in items:
        item = TrackRecognitionEvidence.from_dict(raw_item)
        if item.context_sha256 != album["context_sha256"]:
            raise ProjectValidationError("Identification evidence context is inconsistent.")
        if item.album_sha256 != album["album_sha256"]:
            raise ProjectValidationError("Identification evidence album is inconsistent.")
        resolved_side = side_by_label.get(item.side_label)
        if resolved_side is None:
            raise ProjectValidationError("Identification evidence names an unknown side.")
        side_comparisons = {
            "side_order": "order",
            "project_sha256": "project_sha256",
            "project_revision": "project_revision",
            "project_state_sha256": "project_state_sha256",
            "source_sha256": "source_sha256",
            "source_sample_rate": "source_sample_rate",
            "speed_state_sha256": "speed_state_sha256",
            "requested_speed_factor": "requested_speed_factor",
            "fingerprint_asetrate_hz": "fingerprint_asetrate_hz",
            "fingerprint_effective_speed_factor": (
                "fingerprint_effective_speed_factor"
            ),
            "fingerprint_speed_transform": "fingerprint_speed_transform",
        }
        for evidence_field, side_field in side_comparisons.items():
            if getattr(item, evidence_field) != resolved_side[side_field]:
                raise ProjectValidationError(
                    f"Identification evidence {evidence_field} is inconsistent."
                )
        track_by_number = cast(
            dict[int, dict[str, Any]],
            resolved_side["track_by_number"],
        )
        resolved_track = track_by_number.get(item.track_number)
        if resolved_track is None or (
            item.start_sample,
            item.end_sample,
            item.track_sha256,
        ) != (
            resolved_track["start_sample"],
            resolved_track["end_sample"],
            resolved_track["track_sha256"],
        ):
            raise ProjectValidationError("Identification evidence track is inconsistent.")
        key = (item.side_order, item.track_number)
        if key in seen_evidence:
            raise ProjectValidationError("Identification evidence repeats a track.")
        seen_evidence.add(key)
        ordered_evidence.append((key, item))
    if ordered_evidence != sorted(ordered_evidence, key=lambda entry: entry[0]):
        raise ProjectValidationError("Identification evidence order is not canonical.")
    normalized_items, normalized_evidence_sha, item_hashes = _bounded_evidence_payload(
        ordered_evidence
    )
    if normalized_items != items or normalized_evidence_sha != evidence_set["sha256"]:
        raise ProjectValidationError("Identification evidence serialization is not canonical.")

    config_wrapper = _object(proposal["config"], "Identification config")
    _strict_keys(config_wrapper, {"values", "sha256"}, "Identification config")
    config_values = _object(config_wrapper["values"], "Identification config values")
    _strict_keys(
        config_values,
        set(AlbumIdentificationConfig.__dataclass_fields__),
        "Identification config values",
    )
    config = AlbumIdentificationConfig(
        minimum_supporting_tracks=config_values["minimum_supporting_tracks"],
        minimum_track_coverage=config_values["minimum_track_coverage"],
        minimum_mean_score=config_values["minimum_mean_score"],
        minimum_rank_margin=config_values["minimum_rank_margin"],
        high_confidence_track_coverage=config_values[
            "high_confidence_track_coverage"
        ],
        high_confidence_mean_score=config_values["high_confidence_mean_score"],
        high_confidence_rank_margin=config_values["high_confidence_rank_margin"],
    )
    config.validate()
    if (
        _digest(config_wrapper["sha256"], "Identification config SHA-256")
        != canonical_json_sha256(config_values)
    ):
        raise ProjectValidationError("Identification config SHA-256 is invalid.")

    expected_aggregates = _aggregate_evidence(ordered_evidence, item_hashes)
    expected_candidates, expected_conflicts = _rank_candidates(
        expected_aggregates,
        total_tracks=track_count,
    )
    candidates = _array(
        proposal["ranked_release_candidates"],
        "Ranked release candidates",
    )
    conflicts = _array(proposal["excluded_conflicts"], "Excluded release conflicts")
    if candidates != expected_candidates or conflicts != expected_conflicts:
        raise ProjectValidationError(
            "Identification ranking is inconsistent with its bound evidence."
        )
    expected_decision = _decision(
        expected_candidates,
        total_sides=side_count,
        config=config,
        has_conflicting_release_facts=bool(expected_conflicts),
    )
    decision = _object(proposal["decision"], "Identification decision")
    if decision != expected_decision:
        raise ProjectValidationError(
            "Identification decision is inconsistent with its ranking."
        )

    pressing = _object(proposal["exact_pressing_review"], "Exact pressing review")
    _strict_keys(
        pressing,
        {
            "proof_status",
            "database_release_id_is_physical_pressing_proof",
            "top_ranked_known_facts",
            "missing_or_unverified_facts",
            "owner_checks_required",
            "manual_candidates",
            "manual_candidates_affect_automatic_ranking",
        },
        "Exact pressing review",
    )
    raw_manual = _array(pressing["manual_candidates"], "Manual release candidates")
    if len(raw_manual) > MAX_MANUAL_CANDIDATES:
        raise ProjectValidationError("Manual release candidate count is unbounded.")
    manual_candidates = [
        ManualReleaseCandidate.from_output_dict(item) for item in raw_manual
    ]
    if pressing != _pressing_review(expected_candidates, manual_candidates):
        raise ProjectValidationError("Exact pressing review is semantically inconsistent.")
    authority = _object(proposal["authority"], "Identification authority")
    expected_authority = {
        "may_modify_album_project": False,
        "may_modify_side_projects": False,
        "may_apply_metadata": False,
        "may_download_or_apply_artwork": False,
        "may_change_topology_speed_or_restoration": False,
        "may_publish": False,
        "human_review_required": True,
        "physical_pressing_proven": False,
    }
    if authority != expected_authority:
        raise ProjectValidationError("Album identification proposal grants unsafe authority.")


__all__ = [
    "ALBUM_IDENTIFICATION_ALGORITHM",
    "ALBUM_IDENTIFICATION_EVIDENCE_SCHEMA",
    "ALBUM_IDENTIFICATION_PROPOSAL_SCHEMA",
    "AlbumIdentificationConfig",
    "AlbumIdentificationContext",
    "AlbumSideIdentificationContext",
    "ManualReleaseCandidate",
    "RecognitionObservation",
    "ReleaseCandidateFacts",
    "TrackRecognitionEvidence",
    "capture_album_identification_context",
    "propose_album_release_identification",
    "validate_album_identification_proposal",
]
