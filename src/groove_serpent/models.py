from __future__ import annotations

import hashlib
import json
import math
import ntpath
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .errors import ProjectValidationError
from .portable_names import portable_name_key
from .validation import strict_finite_number as _strict_finite_number

SCHEMA_VERSION = 4

PROJECT_STATE_SCHEMA = "groove-serpent.project-state/1"
ANALYZER_BASELINE_SCHEMA = "groove-serpent.analyzer-baseline/1"
EDIT_HISTORY_SCHEMA = "groove-serpent.edit-history-entry/1"
CHECKPOINT_SCHEMA = "groove-serpent.checkpoint/1"

MAX_TRACKS = 1_000
MAX_METADATA_ITEMS = 128
MAX_METADATA_KEY_LENGTH = 128
MAX_METADATA_VALUE_LENGTH = 4_096
MAX_TRACK_TEXT_LENGTH = 4_096
MAX_EDIT_HISTORY = 100
MAX_CHECKPOINTS = 20
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_HISTORY_BYTES = 16 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 8 * 1024 * 1024
MAX_HISTORY_SUMMARY_LENGTH = 512
MAX_CHECKPOINT_NAME_LENGTH = 80
MAX_ANALYSIS_RATE = 192_000
MAX_ANALYSIS_WINDOW_MS = 10_000
MAX_SMOOTHING_WINDOWS = 100_000
MAX_WAVEFORM_POINTS = 1_000_000
MAX_PROJECT_REVISION = (1 << 63) - 1
MAX_APP_VERSION_LENGTH = 128
MAX_SOURCE_SAMPLE_RATE = 768_000
MAX_SOURCE_CHANNELS = 64
MAX_SOURCE_SIZE_BYTES = (1 << 63) - 1
MAX_SOURCE_TIMESTAMP_NS = (1 << 63) - 1
MAX_SOURCE_SAMPLE_COUNT = (1 << 63) - 1
MAX_SOURCE_BIT_DEPTH = 64
# A one-hertz source is the longest representable capture. Per-project
# validation derives the tighter limit from this sample-count ceiling and the
# source's actual sample rate.
MAX_SUPPORTED_DURATION_SECONDS = float(MAX_SOURCE_SAMPLE_COUNT)
# Historical project JSON rounds human-readable seconds independently from the
# authoritative integer sample coordinate. Ten milliseconds accepts that
# harmless display rounding while still rejecting a materially different cut.
SAMPLE_TIME_TOLERANCE_SECONDS = 0.010
# Serialized analyzer levels are dBFS-like evidence. The broad limits leave
# ample room for future calibrated inputs while refusing meaningless finite
# magnitudes that can destabilize derived arithmetic.
MIN_ANALYSIS_LEVEL_DB = -1_000.0
MAX_ANALYSIS_LEVEL_DB = 1_000.0
MAX_ANALYSIS_CONTRAST_DB = 2_000.0

EDIT_ACTION_KINDS = frozenset(
    {
        "analysis",
        "move_marker",
        "add_marker",
        "remove_marker",
        "split_track",
        "merge_tracks",
        "edit_track",
        "edit_metadata",
        "topology_refit",
        "restore_analyzer",
        "restore_checkpoint",
        "undo",
        "redo",
        "batch_edit",
        "save",
    }
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectValidationError(f"{label} must be a JSON object.")
    if any(not isinstance(key, str) for key in value):
        raise ProjectValidationError(f"{label} keys must be text.")
    return value


def _require_exact_keys(
    data: dict[str, Any], *, required: set[str], optional: set[str], label: str
) -> None:
    missing = required - data.keys()
    if missing:
        raise ProjectValidationError(
            f"{label} is missing required field(s): {', '.join(sorted(missing))}."
        )
    unexpected = data.keys() - required - optional
    if unexpected:
        raise ProjectValidationError(
            f"{label} contains unexpected field(s): {', '.join(sorted(unexpected))}."
        )


def _validate_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ProjectValidationError(f"{label} must be non-empty ISO-8601 text.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectValidationError(f"{label} must be valid ISO-8601 text.") from exc
    if parsed.tzinfo is None:
        raise ProjectValidationError(f"{label} must include a timezone.")
    return value


def _validate_digest(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ProjectValidationError(f"{label} must be text.")
    if allow_empty and value == "":
        return value
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ProjectValidationError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _validate_checkpoint_name(value: Any, label: str = "Checkpoint name") -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_CHECKPOINT_NAME_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise ProjectValidationError(
            f"{label} must contain 1-{MAX_CHECKPOINT_NAME_LENGTH} printable characters."
        )
    return value


def _validated_metadata(value: Any, label: str = "Project metadata") -> dict[str, str]:
    data = _require_mapping(value, label)
    if len(data) > MAX_METADATA_ITEMS:
        raise ProjectValidationError(
            f"{label} cannot contain more than {MAX_METADATA_ITEMS} entries."
        )
    for key, item in data.items():
        if not key or len(key) > MAX_METADATA_KEY_LENGTH:
            raise ProjectValidationError(
                f"{label} keys must contain 1-{MAX_METADATA_KEY_LENGTH} characters."
            )
        if not isinstance(item, str):
            raise ProjectValidationError(f"{label} keys and values must be text.")
        if len(item) > MAX_METADATA_VALUE_LENGTH:
            raise ProjectValidationError(
                f"{label} values cannot exceed {MAX_METADATA_VALUE_LENGTH} characters."
            )
    return dict(data)


def _seconds_to_sample_count(
    value: Any,
    sample_rate: int,
    label: str,
    *,
    maximum: int = MAX_SOURCE_SAMPLE_COUNT,
) -> int:
    """Convert bounded seconds to samples without leaking float overflow."""

    seconds = _strict_finite_number(value, label)
    if seconds < 0 or seconds > maximum / sample_rate:
        raise ProjectValidationError(f"{label} is outside the supported range.")
    try:
        scaled = seconds * sample_rate
        if not math.isfinite(scaled):
            raise OverflowError("non-finite derived sample count")
        result = int(round(scaled))
    except (OverflowError, TypeError, ValueError) as exc:
        raise ProjectValidationError(
            f"{label} is outside the supported range."
        ) from exc
    if not 0 <= result <= maximum:
        raise ProjectValidationError(f"{label} is outside the supported range.")
    return result


@dataclass(slots=True)
class AudioSource:
    path: str
    filename: str
    size_bytes: int
    modified_ns: int
    duration_seconds: float
    sample_rate: int
    channels: int
    codec_name: str
    bits_per_raw_sample: int | None = None
    sample_format: str | None = None
    sample_count: int | None = None
    sha256: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> "AudioSource":
        data = _require_mapping(data, "Audio source")
        _require_exact_keys(
            data,
            required=set(cls.__dataclass_fields__),
            optional=set(),
            label="Audio source",
        )
        return cls(**data)


@dataclass(slots=True)
class AnalysisSettings:
    analysis_rate: int = 8_000
    window_ms: int = 50
    smoothing_windows: int = 5
    threshold_margin_db: float = 6.0
    min_gap_seconds: float = 0.75
    max_gap_seconds: float = 15.0
    min_track_seconds: float = 30.0
    active_run_seconds: float = 0.45
    # Vinyl edges are deliberately conservative: leaving a few seconds for
    # review is reversible, while silently removing a quiet intro or outro is not.
    lead_in_seconds: float = 8.0
    tail_seconds: float = 20.0
    auto_boundary_score: float = 0.55
    waveform_points: int = 4_000

    def validate(self) -> None:
        integer_values: dict[str, tuple[Any, int]] = {
            "analysis rate": (self.analysis_rate, MAX_ANALYSIS_RATE),
            "analysis window length": (self.window_ms, MAX_ANALYSIS_WINDOW_MS),
            "smoothing windows": (self.smoothing_windows, MAX_SMOOTHING_WINDOWS),
            "waveform point count": (self.waveform_points, MAX_WAVEFORM_POINTS),
        }
        for integer_label, (integer_value, maximum) in integer_values.items():
            if isinstance(integer_value, bool) or not isinstance(integer_value, int):
                raise ProjectValidationError(
                    f"Analysis {integer_label} must be an integer."
                )
            _strict_finite_number(integer_value, f"Analysis {integer_label}")
            if integer_value > maximum:
                raise ProjectValidationError(
                    f"Analysis {integer_label} cannot exceed {maximum}."
                )
        numeric_values: dict[str, Any] = {
            "threshold margin": self.threshold_margin_db,
            "minimum gap": self.min_gap_seconds,
            "maximum gap": self.max_gap_seconds,
            "minimum track": self.min_track_seconds,
            "active run": self.active_run_seconds,
            "lead-in": self.lead_in_seconds,
            "tail": self.tail_seconds,
            "automatic boundary score": self.auto_boundary_score,
        }
        for numeric_label, numeric_value in numeric_values.items():
            _strict_finite_number(numeric_value, f"Analysis {numeric_label}")
        temporal_values = {
            "minimum gap": self.min_gap_seconds,
            "maximum gap": self.max_gap_seconds,
            "minimum track": self.min_track_seconds,
            "active run": self.active_run_seconds,
            "lead-in": self.lead_in_seconds,
            "tail": self.tail_seconds,
        }
        for temporal_label, temporal_value in temporal_values.items():
            if temporal_value > MAX_SUPPORTED_DURATION_SECONDS:
                raise ProjectValidationError(
                    f"Analysis {temporal_label} is outside the supported range."
                )
        if not MIN_ANALYSIS_LEVEL_DB <= self.threshold_margin_db <= MAX_ANALYSIS_LEVEL_DB:
            raise ProjectValidationError(
                "Analysis threshold margin is outside the supported dB range."
            )
        if self.analysis_rate <= 0:
            raise ProjectValidationError("Analysis rate must be positive.")
        if self.window_ms <= 0:
            raise ProjectValidationError("Analysis window length must be positive.")
        if self.smoothing_windows < 1:
            raise ProjectValidationError("Smoothing windows must be at least 1.")
        if self.min_gap_seconds <= 0:
            raise ProjectValidationError("Minimum gap length must be positive.")
        if self.max_gap_seconds < self.min_gap_seconds:
            raise ProjectValidationError(
                "Maximum gap length cannot be shorter than the minimum gap."
            )
        if self.min_track_seconds <= 0:
            raise ProjectValidationError("Minimum track length must be positive.")
        if self.active_run_seconds <= 0:
            raise ProjectValidationError("Active run length must be positive.")
        if self.lead_in_seconds < 0 or self.tail_seconds < 0:
            raise ProjectValidationError("Lead-in and tail retention cannot be negative.")
        if not 0.0 <= self.auto_boundary_score <= 1.0:
            raise ProjectValidationError("Automatic boundary score must be between 0 and 1.")
        if self.waveform_points < 1:
            raise ProjectValidationError("Waveform point count must be positive.")

    @classmethod
    def from_dict(cls, data: Any) -> "AnalysisSettings":
        data = _require_mapping(data, "Analysis settings")
        _require_exact_keys(
            data,
            required=set(),
            optional=set(cls.__dataclass_fields__),
            label="Analysis settings",
        )
        settings = cls(**data)
        settings.validate()
        return settings


@dataclass(slots=True)
class BoundaryCandidate:
    start_seconds: float
    end_seconds: float
    cut_seconds: float
    cut_sample: int
    duration_seconds: float
    minimum_db: float
    mean_db: float
    contrast_db: float
    score: float
    selected: bool = False

    def validate(self) -> None:
        if isinstance(self.cut_sample, bool) or not isinstance(self.cut_sample, int):
            raise ProjectValidationError(
                "A boundary candidate cut sample must be an integer."
            )
        _strict_finite_number(
            self.cut_sample, "Boundary candidate cut sample"
        )
        if not 0 <= self.cut_sample <= MAX_SOURCE_SAMPLE_COUNT:
            raise ProjectValidationError(
                "A boundary candidate cut sample is outside the supported range."
            )
        numeric_values: tuple[tuple[str, Any], ...] = (
            ("start time", self.start_seconds),
            ("end time", self.end_seconds),
            ("cut time", self.cut_seconds),
            ("duration", self.duration_seconds),
            ("minimum level", self.minimum_db),
            ("mean level", self.mean_db),
            ("contrast", self.contrast_db),
            ("score", self.score),
        )
        for label, value in numeric_values:
            _strict_finite_number(value, f"Boundary candidate {label}")
        temporal_values = (
            self.start_seconds,
            self.end_seconds,
            self.cut_seconds,
            self.duration_seconds,
        )
        if any(
            value < 0 or value > MAX_SUPPORTED_DURATION_SECONDS
            for value in temporal_values
        ):
            raise ProjectValidationError(
                "A boundary candidate time is outside the supported range."
            )
        if not self.start_seconds <= self.cut_seconds <= self.end_seconds:
            raise ProjectValidationError("A boundary candidate has invalid bounds.")
        if self.end_seconds < self.start_seconds or self.duration_seconds < 0:
            raise ProjectValidationError("A boundary candidate has invalid bounds.")
        if not (
            MIN_ANALYSIS_LEVEL_DB
            <= self.minimum_db
            <= MAX_ANALYSIS_LEVEL_DB
            and MIN_ANALYSIS_LEVEL_DB
            <= self.mean_db
            <= MAX_ANALYSIS_LEVEL_DB
        ):
            raise ProjectValidationError(
                "Boundary candidate levels are outside the supported dB range."
            )
        if not 0.0 <= self.contrast_db <= MAX_ANALYSIS_CONTRAST_DB:
            raise ProjectValidationError(
                "Boundary candidate contrast is outside the supported dB range."
            )
        if not 0.0 <= self.score <= 1.0:
            raise ProjectValidationError(
                "Boundary candidate score must be between 0 and 1."
            )

    @classmethod
    def from_dict(cls, data: Any) -> "BoundaryCandidate":
        data = _require_mapping(data, "Boundary candidate")
        _require_exact_keys(
            data,
            required={
                "start_seconds",
                "end_seconds",
                "cut_seconds",
                "cut_sample",
                "duration_seconds",
                "minimum_db",
                "mean_db",
                "contrast_db",
                "score",
            },
            optional={"selected"},
            label="Boundary candidate",
        )
        candidate = cls(**data)
        candidate.validate()
        return candidate


@dataclass(slots=True)
class Track:
    number: int
    title: str
    start_sample: int
    end_sample: int
    start_seconds: float
    end_seconds: float
    confidence: float = 0.0
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    year: str = ""
    genre: str = ""
    side: str = ""
    expected_duration_seconds: float | None = None
    musicbrainz_recording_id: str = ""
    musicbrainz_track_id: str = ""

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)

    @classmethod
    def from_dict(cls, data: Any) -> "Track":
        data = _require_mapping(data, "Track")
        _require_exact_keys(
            data,
            required=set(cls.__dataclass_fields__),
            optional=set(),
            label="Track",
        )
        return cls(**data)


_TRACK_REQUIRED_FIELDS = {
    "number",
    "title",
    "start_sample",
    "end_sample",
    "start_seconds",
    "end_seconds",
}
_TRACK_FIELDS = set(Track.__dataclass_fields__)


def _strict_track_from_dict(value: Any, label: str) -> Track:
    data = _require_mapping(value, label)
    _require_exact_keys(
        data,
        required=_TRACK_REQUIRED_FIELDS,
        optional=_TRACK_FIELDS - _TRACK_REQUIRED_FIELDS,
        label=label,
    )
    return Track.from_dict(data)


def _canonical_state_bytes(tracks: list[Track], metadata: dict[str, str]) -> bytes:
    payload = {
        "schema": PROJECT_STATE_SCHEMA,
        "tracks": [asdict(track) for track in tracks],
        "metadata": {key: metadata[key] for key in sorted(metadata)},
    }
    try:
        rendered = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ProjectValidationError(f"Project state is not canonical JSON: {exc}") from exc
    return rendered.encode("utf-8")


def project_state_sha256(tracks: list[Track], metadata: dict[str, str]) -> str:
    """Return the deterministic hash for an exact editable project state.

    The hash covers every serialized track field and sorted project metadata.
    Source identity is bound separately by baseline, history, and checkpoint
    records so that the same edits cannot silently move between captures.
    """

    return hashlib.sha256(_canonical_state_bytes(tracks, metadata)).hexdigest()


@dataclass(slots=True)
class ProjectState:
    tracks: list[Track]
    metadata: dict[str, str]
    schema: str = PROJECT_STATE_SCHEMA

    @classmethod
    def capture(cls, tracks: list[Track], metadata: dict[str, str]) -> "ProjectState":
        return cls(
            tracks=[Track.from_dict(asdict(track)) for track in tracks],
            metadata=dict(metadata),
        )

    @classmethod
    def from_dict(cls, value: Any, label: str = "Project state") -> "ProjectState":
        data = _require_mapping(value, label)
        _require_exact_keys(
            data,
            required={"schema", "tracks", "metadata"},
            optional=set(),
            label=label,
        )
        if data["schema"] != PROJECT_STATE_SCHEMA:
            raise ProjectValidationError(
                f"{label} schema must be '{PROJECT_STATE_SCHEMA}'."
            )
        raw_tracks = data["tracks"]
        if not isinstance(raw_tracks, list):
            raise ProjectValidationError(f"{label} tracks must be a JSON array.")
        if not raw_tracks or len(raw_tracks) > MAX_TRACKS:
            raise ProjectValidationError(
                f"{label} must contain 1-{MAX_TRACKS} tracks."
            )
        state = cls(
            tracks=[
                _strict_track_from_dict(item, f"{label} track {index}")
                for index, item in enumerate(raw_tracks, start=1)
            ],
            metadata=_validated_metadata(data["metadata"], f"{label} metadata"),
        )
        if state.serialized_size > MAX_STATE_BYTES:
            raise ProjectValidationError(
                f"{label} exceeds the {MAX_STATE_BYTES}-byte state limit."
            )
        return state

    @property
    def sha256(self) -> str:
        return project_state_sha256(self.tracks, self.metadata)

    @property
    def serialized_size(self) -> int:
        return len(_canonical_state_bytes(self.tracks, self.metadata))


@dataclass(slots=True)
class AnalyzerBaseline:
    state: ProjectState
    state_sha256: str
    source_sha256: str
    schema: str = ANALYZER_BASELINE_SCHEMA

    @classmethod
    def capture(
        cls, tracks: list[Track], metadata: dict[str, str], source_sha256: str
    ) -> "AnalyzerBaseline":
        state = ProjectState.capture(tracks, metadata)
        return cls(
            state=state,
            state_sha256=state.sha256,
            source_sha256=source_sha256.lower(),
        )

    @classmethod
    def from_dict(cls, value: Any) -> "AnalyzerBaseline":
        data = _require_mapping(value, "Analyzer baseline")
        _require_exact_keys(
            data,
            required={"schema", "state", "state_sha256", "source_sha256"},
            optional=set(),
            label="Analyzer baseline",
        )
        if data["schema"] != ANALYZER_BASELINE_SCHEMA:
            raise ProjectValidationError(
                f"Analyzer baseline schema must be '{ANALYZER_BASELINE_SCHEMA}'."
            )
        return cls(
            state=ProjectState.from_dict(data["state"], "Analyzer baseline state"),
            state_sha256=_validate_digest(
                data["state_sha256"], "Analyzer baseline state SHA-256"
            ),
            source_sha256=_validate_digest(
                data["source_sha256"],
                "Analyzer baseline source SHA-256",
                allow_empty=True,
            ),
        )

    @property
    def tracks(self) -> list[Track]:
        return self.state.tracks

    @property
    def metadata(self) -> dict[str, str]:
        return self.state.metadata


@dataclass(slots=True)
class EditHistoryEntry:
    sequence: int
    timestamp: str
    action: str
    summary: str
    before: ProjectState
    after: ProjectState
    before_sha256: str
    after_sha256: str
    source_sha256: str
    schema: str = EDIT_HISTORY_SCHEMA

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        action: str,
        summary: str,
        before: ProjectState,
        after: ProjectState,
        source_sha256: str,
        timestamp: str | None = None,
    ) -> "EditHistoryEntry":
        return cls(
            sequence=sequence,
            timestamp=timestamp or utc_now_iso(),
            action=action,
            summary=summary,
            before=ProjectState.capture(before.tracks, before.metadata),
            after=ProjectState.capture(after.tracks, after.metadata),
            before_sha256=before.sha256,
            after_sha256=after.sha256,
            source_sha256=source_sha256.lower(),
        )

    @classmethod
    def from_dict(cls, value: Any, index: int) -> "EditHistoryEntry":
        label = f"Edit history entry {index}"
        data = _require_mapping(value, label)
        _require_exact_keys(
            data,
            required={
                "schema",
                "sequence",
                "timestamp",
                "action",
                "summary",
                "before",
                "after",
                "before_sha256",
                "after_sha256",
                "source_sha256",
            },
            optional=set(),
            label=label,
        )
        if data["schema"] != EDIT_HISTORY_SCHEMA:
            raise ProjectValidationError(
                f"{label} schema must be '{EDIT_HISTORY_SCHEMA}'."
            )
        if type(data["sequence"]) is not int or data["sequence"] <= 0:
            raise ProjectValidationError(f"{label} sequence must be a positive integer.")
        if data["action"] not in EDIT_ACTION_KINDS:
            raise ProjectValidationError(f"{label} action is not supported.")
        if (
            not isinstance(data["summary"], str)
            or not data["summary"]
            or len(data["summary"]) > MAX_HISTORY_SUMMARY_LENGTH
        ):
            raise ProjectValidationError(
                f"{label} summary must contain 1-{MAX_HISTORY_SUMMARY_LENGTH} characters."
            )
        return cls(
            sequence=data["sequence"],
            timestamp=_validate_timestamp(data["timestamp"], f"{label} timestamp"),
            action=data["action"],
            summary=data["summary"],
            before=ProjectState.from_dict(data["before"], f"{label} before state"),
            after=ProjectState.from_dict(data["after"], f"{label} after state"),
            before_sha256=_validate_digest(
                data["before_sha256"], f"{label} before SHA-256"
            ),
            after_sha256=_validate_digest(
                data["after_sha256"], f"{label} after SHA-256"
            ),
            source_sha256=_validate_digest(
                data["source_sha256"], f"{label} source SHA-256", allow_empty=True
            ),
        )


@dataclass(slots=True)
class ProjectCheckpoint:
    name: str
    created_at: str
    project_revision: int
    state: ProjectState
    state_sha256: str
    source_sha256: str
    schema: str = CHECKPOINT_SCHEMA

    @classmethod
    def capture(
        cls,
        *,
        name: str,
        project_revision: int,
        tracks: list[Track],
        metadata: dict[str, str],
        source_sha256: str,
        created_at: str | None = None,
    ) -> "ProjectCheckpoint":
        state = ProjectState.capture(tracks, metadata)
        return cls(
            name=name,
            created_at=created_at or utc_now_iso(),
            project_revision=project_revision,
            state=state,
            state_sha256=state.sha256,
            source_sha256=source_sha256.lower(),
        )

    @classmethod
    def from_dict(cls, value: Any, index: int) -> "ProjectCheckpoint":
        label = f"Checkpoint {index}"
        data = _require_mapping(value, label)
        _require_exact_keys(
            data,
            required={
                "schema",
                "name",
                "created_at",
                "project_revision",
                "state",
                "state_sha256",
                "source_sha256",
            },
            optional=set(),
            label=label,
        )
        if data["schema"] != CHECKPOINT_SCHEMA:
            raise ProjectValidationError(
                f"{label} schema must be '{CHECKPOINT_SCHEMA}'."
            )
        name = _validate_checkpoint_name(data["name"], f"{label} name")
        revision = data["project_revision"]
        if type(revision) is not int or revision <= 0:
            raise ProjectValidationError(f"{label} project revision must be a positive integer.")
        return cls(
            name=name,
            created_at=_validate_timestamp(data["created_at"], f"{label} creation time"),
            project_revision=revision,
            state=ProjectState.from_dict(data["state"], f"{label} state"),
            state_sha256=_validate_digest(
                data["state_sha256"], f"{label} state SHA-256"
            ),
            source_sha256=_validate_digest(
                data["source_sha256"], f"{label} source SHA-256", allow_empty=True
            ),
        )


def _validate_tracks_for_source(
    tracks: Any, source: AudioSource, *, label: str
) -> None:
    if not isinstance(tracks, list) or not tracks:
        raise ProjectValidationError(f"{label} must contain at least one track.")
    if len(tracks) > MAX_TRACKS:
        raise ProjectValidationError(
            f"{label} cannot contain more than {MAX_TRACKS} tracks."
        )
    maximum_sample = (
        source.sample_count
        if source.sample_count is not None
        else min(
            MAX_SOURCE_SAMPLE_COUNT,
            _seconds_to_sample_count(
                source.duration_seconds,
                source.sample_rate,
                "The source duration",
            )
            + 2,
        )
    )
    previous_end = -1
    for expected_number, track in enumerate(tracks, start=1):
        if not isinstance(track, Track):
            raise ProjectValidationError(f"{label} track {expected_number} is invalid.")
        if isinstance(track.number, bool) or not isinstance(track.number, int):
            raise ProjectValidationError(f"{label} track numbers must be integers.")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (track.start_sample, track.end_sample)
        ):
            raise ProjectValidationError(
                f"{label} track {expected_number} sample bounds must be integers."
            )
        track_text = {
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "album artist": track.album_artist,
            "year": track.year,
            "genre": track.genre,
            "side": track.side,
            "MusicBrainz recording ID": track.musicbrainz_recording_id,
            "MusicBrainz track ID": track.musicbrainz_track_id,
        }
        for text_label, value in track_text.items():
            if not isinstance(value, str):
                raise ProjectValidationError(
                    f"{label} track {expected_number} {text_label} must be text."
                )
            if len(value) > MAX_TRACK_TEXT_LENGTH:
                raise ProjectValidationError(
                    f"{label} track {expected_number} {text_label} exceeds "
                    f"{MAX_TRACK_TEXT_LENGTH} characters."
                )
        if track.number != expected_number:
            raise ProjectValidationError(
                f"{label} track numbers must be consecutive and start at 1."
            )
        if track.start_sample < 0 or track.end_sample <= track.start_sample:
            raise ProjectValidationError(
                f"{label} track {track.number} has invalid sample bounds."
            )
        if track.end_sample > maximum_sample:
            raise ProjectValidationError(
                f"{label} track {track.number} extends past the source audio."
            )
        for numeric_label, numeric_value in (
            ("start time", track.start_seconds),
            ("end time", track.end_seconds),
            ("confidence", track.confidence),
        ):
            _strict_finite_number(
                numeric_value, f"{label} track {track.number} {numeric_label}"
            )
        if not 0.0 <= track.confidence <= 1.0:
            raise ProjectValidationError(
                f"{label} track {track.number} confidence must be between 0 and 1."
            )
        if track.expected_duration_seconds is not None:
            expected_duration = _strict_finite_number(
                track.expected_duration_seconds,
                f"{label} track {track.number} expected duration",
            )
            if expected_duration <= 0:
                raise ProjectValidationError(
                    f"{label} track {track.number} expected duration must be positive "
                    "and finite."
                )
            if expected_duration > MAX_SUPPORTED_DURATION_SECONDS:
                raise ProjectValidationError(
                    f"{label} track {track.number} expected duration is outside "
                    "the supported range."
                )
        if previous_end >= 0 and track.start_sample != previous_end:
            raise ProjectValidationError(
                f"{label} track {track.number} does not begin where the previous track ends."
            )
        expected_start = track.start_sample / source.sample_rate
        expected_end = track.end_sample / source.sample_rate
        if abs(track.start_seconds - expected_start) > SAMPLE_TIME_TOLERANCE_SECONDS:
            raise ProjectValidationError(
                f"{label} track {track.number} start time and sample disagree."
            )
        if abs(track.end_seconds - expected_end) > SAMPLE_TIME_TOLERANCE_SECONDS:
            raise ProjectValidationError(
                f"{label} track {track.number} end time and sample disagree."
            )
        previous_end = track.end_sample


def _validate_project_state(
    state: Any, source: AudioSource, *, label: str
) -> None:
    if not isinstance(state, ProjectState) or state.schema != PROJECT_STATE_SCHEMA:
        raise ProjectValidationError(f"{label} has an invalid project-state schema.")
    _validate_tracks_for_source(state.tracks, source, label=f"{label} tracks")
    _validated_metadata(state.metadata, f"{label} metadata")
    if state.serialized_size > MAX_STATE_BYTES:
        raise ProjectValidationError(
            f"{label} exceeds the {MAX_STATE_BYTES}-byte state limit."
        )


@dataclass(slots=True)
class AnalysisSummary:
    music_start_seconds: float
    music_end_seconds: float
    noise_floor_db: float
    silence_threshold_db: float
    active_threshold_db: float
    envelope_window_seconds: float
    candidates: list[BoundaryCandidate] = field(default_factory=list)
    waveform: list[float] = field(default_factory=list)

    def validate(self) -> None:
        numeric_values: tuple[tuple[str, Any], ...] = (
            ("music start", self.music_start_seconds),
            ("music end", self.music_end_seconds),
            ("noise floor", self.noise_floor_db),
            ("silence threshold", self.silence_threshold_db),
            ("active threshold", self.active_threshold_db),
            ("envelope window", self.envelope_window_seconds),
        )
        for label, value in numeric_values:
            _strict_finite_number(value, f"Analysis summary {label}")
        if (
            self.music_start_seconds < 0
            or self.music_start_seconds > MAX_SUPPORTED_DURATION_SECONDS
            or self.music_end_seconds > MAX_SUPPORTED_DURATION_SECONDS
            or self.music_end_seconds <= self.music_start_seconds
        ):
            raise ProjectValidationError("Analysis summary music bounds are invalid.")
        if self.envelope_window_seconds <= 0:
            raise ProjectValidationError("Analysis envelope window must be positive.")
        if self.envelope_window_seconds > MAX_SUPPORTED_DURATION_SECONDS:
            raise ProjectValidationError(
                "Analysis envelope window is outside the supported range."
            )
        for label, value in (
            ("noise floor", self.noise_floor_db),
            ("silence threshold", self.silence_threshold_db),
            ("active threshold", self.active_threshold_db),
        ):
            if not MIN_ANALYSIS_LEVEL_DB <= value <= MAX_ANALYSIS_LEVEL_DB:
                raise ProjectValidationError(
                    f"Analysis summary {label} is outside the supported dB range."
                )
        if not isinstance(self.candidates, list):
            raise ProjectValidationError(
                "Analysis summary candidates must be a JSON array."
            )
        for candidate in self.candidates:
            if not isinstance(candidate, BoundaryCandidate):
                raise ProjectValidationError(
                    "Analysis summary candidates must use boundary candidate models."
                )
            candidate.validate()
        if not isinstance(self.waveform, list):
            raise ProjectValidationError("Analysis waveform must be a JSON array.")
        for value in self.waveform:
            normalized = _strict_finite_number(value, "Analysis waveform value")
            if not 0.0 <= normalized <= 1.0:
                raise ProjectValidationError(
                    "Analysis waveform values must be between 0 and 1."
                )

    @classmethod
    def from_dict(cls, data: Any) -> "AnalysisSummary":
        data = _require_mapping(data, "Analysis summary")
        _require_exact_keys(
            data,
            required={
                "music_start_seconds",
                "music_end_seconds",
                "noise_floor_db",
                "silence_threshold_db",
                "active_threshold_db",
                "envelope_window_seconds",
            },
            optional={"candidates", "waveform"},
            label="Analysis summary",
        )
        raw_candidates = data.get("candidates", [])
        if not isinstance(raw_candidates, list):
            raise ProjectValidationError(
                "Analysis summary candidates must be a JSON array."
            )
        raw_waveform = data.get("waveform", [])
        if not isinstance(raw_waveform, list):
            raise ProjectValidationError("Analysis waveform must be a JSON array.")
        candidates = [BoundaryCandidate.from_dict(item) for item in raw_candidates]
        summary = cls(
            music_start_seconds=_strict_finite_number(
                data["music_start_seconds"], "Analysis summary music start"
            ),
            music_end_seconds=_strict_finite_number(
                data["music_end_seconds"], "Analysis summary music end"
            ),
            noise_floor_db=_strict_finite_number(
                data["noise_floor_db"], "Analysis summary noise floor"
            ),
            silence_threshold_db=_strict_finite_number(
                data["silence_threshold_db"], "Analysis summary silence threshold"
            ),
            active_threshold_db=_strict_finite_number(
                data["active_threshold_db"], "Analysis summary active threshold"
            ),
            envelope_window_seconds=_strict_finite_number(
                data["envelope_window_seconds"], "Analysis summary envelope window"
            ),
            candidates=candidates,
            waveform=[
                _strict_finite_number(value, "Analysis waveform value")
                for value in raw_waveform
            ],
        )
        summary.validate()
        return summary


def _validate_analysis_for_source(
    analysis: AnalysisSummary, source: AudioSource
) -> None:
    """Bind every serialized analysis coordinate to one source geometry."""

    analysis.validate()
    source_duration = source.duration_seconds
    if not (
        0.0
        <= analysis.music_start_seconds
        < analysis.music_end_seconds
        <= source_duration
    ):
        raise ProjectValidationError(
            "Analysis music bounds must remain within the source audio."
        )
    if analysis.envelope_window_seconds > source_duration:
        raise ProjectValidationError(
            "Analysis envelope window cannot exceed the source duration."
        )

    duration_tolerance = max(
        SAMPLE_TIME_TOLERANCE_SECONDS,
        analysis.envelope_window_seconds + 1e-12,
    )
    source_sample_count = (
        source.sample_count
        if source.sample_count is not None
        else _seconds_to_sample_count(
            source_duration, source.sample_rate, "The source duration"
        )
    )
    for index, candidate in enumerate(analysis.candidates, start=1):
        label = f"Boundary candidate {index}"
        if not (
            0.0
            <= candidate.start_seconds
            <= candidate.cut_seconds
            <= candidate.end_seconds
            <= source_duration
        ):
            raise ProjectValidationError(
                f"{label} must remain within the source audio."
            )
        if candidate.duration_seconds > source_duration:
            raise ProjectValidationError(
                f"{label} duration exceeds the source audio."
            )
        measured_span = candidate.end_seconds - candidate.start_seconds
        if abs(candidate.duration_seconds - measured_span) > duration_tolerance:
            raise ProjectValidationError(
                f"{label} duration disagrees with its measured bounds."
            )
        if not 0 <= candidate.cut_sample <= source_sample_count:
            raise ProjectValidationError(
                f"{label} cut sample is outside the source audio."
            )
        cut_from_sample = candidate.cut_sample / source.sample_rate
        sample_tolerance = (
            0.5 / source.sample_rate
            + 4.0
            * max(
                math.ulp(candidate.cut_seconds),
                math.ulp(cut_from_sample),
            )
        )
        if (
            abs(candidate.cut_seconds - cut_from_sample)
            > sample_tolerance
        ):
            raise ProjectValidationError(
                f"{label} cut time and sample disagree by more than "
                "half a source sample."
            )


@dataclass(slots=True)
class Project:
    source: AudioSource
    settings: AnalysisSettings
    analysis: AnalysisSummary
    tracks: list[Track]
    metadata: dict[str, str] = field(default_factory=dict)
    analyzer_baseline: AnalyzerBaseline | None = None
    edit_history: list[EditHistoryEntry] = field(default_factory=list)
    checkpoints: list[ProjectCheckpoint] = field(default_factory=list)
    revision: int = 1
    schema_version: int = SCHEMA_VERSION
    app_version: str = __version__
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if (
            self.analyzer_baseline is None
            and isinstance(self.source, AudioSource)
            and isinstance(self.source.sha256, str)
            and isinstance(self.tracks, list)
            and isinstance(self.metadata, dict)
        ):
            self.analyzer_baseline = AnalyzerBaseline.capture(
                self.tracks, self.metadata, self.source.sha256
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        data = _require_mapping(data, "Project")
        schema_value = data.get("schema_version")
        if type(schema_value) is not int:
            raise ProjectValidationError("The project schema version must be an integer.")
        if schema_value != SCHEMA_VERSION:
            if schema_value in {1, 2, 3}:
                raise ProjectValidationError(
                    f"Project schema {schema_value} is legacy. Run "
                    "'groove-serpent project migrate PROJECT' before opening it."
                )
            raise ProjectValidationError(
                f"Unsupported project schema {schema_value}; expected {SCHEMA_VERSION}."
            )
        required_fields = {
            "source",
            "settings",
            "analysis",
            "tracks",
            "metadata",
            "analyzer_baseline",
            "edit_history",
            "checkpoints",
            "revision",
            "schema_version",
            "app_version",
            "created_at",
            "updated_at",
        }
        _require_exact_keys(
            data,
            required=required_fields,
            optional=set(),
            label="Project",
        )
        settings_data = _require_mapping(data["settings"], "Analysis settings")
        _require_exact_keys(
            settings_data,
            required=set(AnalysisSettings.__dataclass_fields__),
            optional=set(),
            label="Analysis settings",
        )
        analysis_data = _require_mapping(data["analysis"], "Analysis summary")
        _require_exact_keys(
            analysis_data,
            required=set(AnalysisSummary.__dataclass_fields__),
            optional=set(),
            label="Analysis summary",
        )
        raw_candidates = analysis_data["candidates"]
        if not isinstance(raw_candidates, list):
            raise ProjectValidationError(
                "Analysis summary candidates must be a JSON array."
            )
        for index, item in enumerate(raw_candidates, start=1):
            candidate_data = _require_mapping(
                item, f"Boundary candidate {index}"
            )
            _require_exact_keys(
                candidate_data,
                required=set(BoundaryCandidate.__dataclass_fields__),
                optional=set(),
                label=f"Boundary candidate {index}",
            )
        revision_value = data["revision"]
        if type(revision_value) is not int or revision_value <= 0:
            raise ProjectValidationError("The project revision must be a positive integer.")
        source = AudioSource.from_dict(data["source"])
        raw_tracks = data["tracks"]
        if not isinstance(raw_tracks, list):
            raise ProjectValidationError("Project tracks must be a JSON array.")
        tracks = [
            _strict_track_from_dict(item, f"Project track {index}")
            for index, item in enumerate(raw_tracks, start=1)
        ]
        metadata = _validated_metadata(data["metadata"])
        analyzer_baseline = AnalyzerBaseline.from_dict(data["analyzer_baseline"])

        raw_history = data["edit_history"]
        if not isinstance(raw_history, list):
            raise ProjectValidationError("Project edit history must be a JSON array.")
        if len(raw_history) > MAX_EDIT_HISTORY:
            raise ProjectValidationError(
                f"Project edit history cannot exceed {MAX_EDIT_HISTORY} entries."
            )
        edit_history = [
            EditHistoryEntry.from_dict(item, index)
            for index, item in enumerate(raw_history, start=1)
        ]

        raw_checkpoints = data["checkpoints"]
        if not isinstance(raw_checkpoints, list):
            raise ProjectValidationError("Project checkpoints must be a JSON array.")
        if len(raw_checkpoints) > MAX_CHECKPOINTS:
            raise ProjectValidationError(
                f"A project cannot contain more than {MAX_CHECKPOINTS} checkpoints."
            )
        checkpoints = [
            ProjectCheckpoint.from_dict(item, index)
            for index, item in enumerate(raw_checkpoints, start=1)
        ]

        app_version = data["app_version"]
        created_at = data["created_at"]
        updated_at = data["updated_at"]
        project = cls(
            source=source,
            settings=AnalysisSettings.from_dict(data["settings"]),
            analysis=AnalysisSummary.from_dict(data["analysis"]),
            tracks=tracks,
            metadata=metadata,
            analyzer_baseline=analyzer_baseline,
            edit_history=edit_history,
            checkpoints=checkpoints,
            revision=revision_value,
            schema_version=SCHEMA_VERSION,
            app_version=app_version,
            created_at=created_at,
            updated_at=updated_at,
        )
        project.validate()
        return project

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ProjectValidationError(
                f"The project schema version must be {SCHEMA_VERSION}."
            )
        if (
            not isinstance(self.app_version, str)
            or not self.app_version
            or len(self.app_version) > MAX_APP_VERSION_LENGTH
            or any(ord(character) < 32 for character in self.app_version)
        ):
            raise ProjectValidationError(
                "The project app version must be non-empty bounded printable text."
            )
        _validate_timestamp(self.created_at, "Project creation time")
        _validate_timestamp(self.updated_at, "Project update time")
        if (
            type(self.revision) is not int
            or not 1 <= self.revision <= MAX_PROJECT_REVISION
        ):
            raise ProjectValidationError("The project revision must be a positive integer.")
        self.settings.validate()
        source_text = {
            "path": self.source.path,
            "filename": self.source.filename,
            "codec name": self.source.codec_name,
        }
        for label, value in source_text.items():
            if not isinstance(value, str) or not value:
                raise ProjectValidationError(f"The source {label} must be non-empty text.")
        if Path(self.source.filename).name != self.source.filename:
            raise ProjectValidationError("The source filename must not contain a directory path.")
        if self.source.sample_format is not None and not isinstance(
            self.source.sample_format, str
        ):
            raise ProjectValidationError("The source sample format must be text when present.")
        if not isinstance(self.source.sha256, str):
            raise ProjectValidationError("The source SHA-256 value must be text.")
        source_integers: dict[str, tuple[Any, int, int]] = {
            "sample rate": (self.source.sample_rate, 1, MAX_SOURCE_SAMPLE_RATE),
            "channel count": (self.source.channels, 1, MAX_SOURCE_CHANNELS),
            "size": (self.source.size_bytes, 0, MAX_SOURCE_SIZE_BYTES),
            "modified time": (
                self.source.modified_ns,
                -MAX_SOURCE_TIMESTAMP_NS,
                MAX_SOURCE_TIMESTAMP_NS,
            ),
        }
        for integer_label, (integer_value, minimum, maximum) in source_integers.items():
            if isinstance(integer_value, bool) or not isinstance(integer_value, int):
                raise ProjectValidationError(
                    f"The source {integer_label} must be an integer."
                )
            _strict_finite_number(integer_value, f"The source {integer_label}")
            if not minimum <= integer_value <= maximum:
                raise ProjectValidationError(
                    f"The source {integer_label} is outside the supported range."
                )
        for optional_label, optional_value, maximum in (
            ("sample count", self.source.sample_count, MAX_SOURCE_SAMPLE_COUNT),
            ("bit depth", self.source.bits_per_raw_sample, MAX_SOURCE_BIT_DEPTH),
        ):
            if optional_value is not None and (
                isinstance(optional_value, bool)
                or not isinstance(optional_value, int)
            ):
                raise ProjectValidationError(
                    f"The source {optional_label} must be an integer when present."
                )
            if optional_value is not None:
                _strict_finite_number(
                    optional_value, f"The source {optional_label}"
                )
                if not 1 <= optional_value <= maximum:
                    raise ProjectValidationError(
                        f"The source {optional_label} is outside the supported range."
                    )
        source_duration = _strict_finite_number(
            self.source.duration_seconds, "The source duration"
        )
        if source_duration <= 0:
            raise ProjectValidationError("The source duration must be positive.")
        expected_sample_count = _seconds_to_sample_count(
            source_duration,
            self.source.sample_rate,
            "The source duration",
        )
        if (
            self.source.sample_count is not None
            and abs(self.source.sample_count - expected_sample_count) > 2
        ):
            raise ProjectValidationError(
                "The source sample count disagrees with its duration and sample rate."
            )
        if self.source.sha256 and (
            len(self.source.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.source.sha256.lower())
        ):
            raise ProjectValidationError("The source SHA-256 value is invalid.")
        _validate_tracks_for_source(self.tracks, self.source, label="Project")
        _validated_metadata(self.metadata)

        if not isinstance(self.analysis, AnalysisSummary):
            raise ProjectValidationError(
                "Project analysis must use the analysis summary model."
            )
        _validate_analysis_for_source(self.analysis, self.source)

        baseline = self.analyzer_baseline
        if (
            not isinstance(baseline, AnalyzerBaseline)
            or baseline.schema != ANALYZER_BASELINE_SCHEMA
        ):
            raise ProjectValidationError("The analyzer baseline schema is invalid.")
        _validate_project_state(
            baseline.state, self.source, label="Analyzer baseline"
        )
        _validate_digest(
            baseline.state_sha256, "Analyzer baseline state SHA-256"
        )
        _validate_digest(
            baseline.source_sha256,
            "Analyzer baseline source SHA-256",
            allow_empty=True,
        )
        if baseline.state_sha256 != baseline.state.sha256:
            raise ProjectValidationError(
                "The analyzer baseline state hash does not match its state."
            )
        expected_source_sha256 = self.source.sha256.lower()
        if baseline.source_sha256 != expected_source_sha256:
            raise ProjectValidationError(
                "The analyzer baseline is bound to a different source capture."
            )

        if not isinstance(self.edit_history, list):
            raise ProjectValidationError("Project edit history must be a list.")
        if len(self.edit_history) > MAX_EDIT_HISTORY:
            raise ProjectValidationError(
                f"Project edit history cannot exceed {MAX_EDIT_HISTORY} entries."
            )
        history_bytes = 0
        previous_entry: EditHistoryEntry | None = None
        for index, entry in enumerate(self.edit_history, start=1):
            label = f"Edit history entry {index}"
            if not isinstance(entry, EditHistoryEntry) or entry.schema != EDIT_HISTORY_SCHEMA:
                raise ProjectValidationError(f"{label} schema is invalid.")
            if type(entry.sequence) is not int or entry.sequence <= 0:
                raise ProjectValidationError(f"{label} sequence must be a positive integer.")
            _validate_timestamp(entry.timestamp, f"{label} timestamp")
            if entry.action not in EDIT_ACTION_KINDS:
                raise ProjectValidationError(f"{label} action is not supported.")
            if (
                not isinstance(entry.summary, str)
                or not entry.summary
                or len(entry.summary) > MAX_HISTORY_SUMMARY_LENGTH
            ):
                raise ProjectValidationError(
                    f"{label} summary must contain 1-{MAX_HISTORY_SUMMARY_LENGTH} characters."
                )
            _validate_project_state(entry.before, self.source, label=f"{label} before state")
            _validate_project_state(entry.after, self.source, label=f"{label} after state")
            _validate_digest(entry.before_sha256, f"{label} before SHA-256")
            _validate_digest(entry.after_sha256, f"{label} after SHA-256")
            _validate_digest(
                entry.source_sha256, f"{label} source SHA-256", allow_empty=True
            )
            if entry.before_sha256 != entry.before.sha256:
                raise ProjectValidationError(f"{label} before-state hash does not match.")
            if entry.after_sha256 != entry.after.sha256:
                raise ProjectValidationError(f"{label} after-state hash does not match.")
            if entry.source_sha256 != expected_source_sha256:
                raise ProjectValidationError(f"{label} is bound to a different source capture.")
            if previous_entry is not None:
                if entry.sequence != previous_entry.sequence + 1:
                    raise ProjectValidationError(
                        "Edit history sequences must be consecutive in retained history."
                    )
                if entry.before_sha256 != previous_entry.after_sha256:
                    raise ProjectValidationError(
                        "Edit history states must form an unbroken hash chain."
                    )
            history_bytes += entry.before.serialized_size + entry.after.serialized_size
            previous_entry = entry
        if history_bytes > MAX_HISTORY_BYTES:
            raise ProjectValidationError(
                f"Project edit history exceeds the {MAX_HISTORY_BYTES}-byte limit."
            )
        if self.edit_history and self.edit_history[-1].after_sha256 != self.state_sha256:
            raise ProjectValidationError(
                "The latest edit-history state does not match the current project state."
            )

        if not isinstance(self.checkpoints, list):
            raise ProjectValidationError("Project checkpoints must be a list.")
        if len(self.checkpoints) > MAX_CHECKPOINTS:
            raise ProjectValidationError(
                f"A project cannot contain more than {MAX_CHECKPOINTS} checkpoints."
            )
        checkpoint_names: set[str] = set()
        checkpoint_bytes = 0
        for index, checkpoint in enumerate(self.checkpoints, start=1):
            label = f"Checkpoint {index}"
            if (
                not isinstance(checkpoint, ProjectCheckpoint)
                or checkpoint.schema != CHECKPOINT_SCHEMA
            ):
                raise ProjectValidationError(f"{label} schema is invalid.")
            _validate_checkpoint_name(checkpoint.name, f"{label} name")
            normalized_name = portable_name_key(checkpoint.name)
            if normalized_name in checkpoint_names:
                raise ProjectValidationError("Checkpoint names must be unique.")
            checkpoint_names.add(normalized_name)
            _validate_timestamp(checkpoint.created_at, f"{label} creation time")
            if (
                type(checkpoint.project_revision) is not int
                or checkpoint.project_revision <= 0
                or checkpoint.project_revision > self.revision
            ):
                raise ProjectValidationError(
                    f"{label} project revision must be a positive revision "
                    "not newer than the project."
                )
            _validate_project_state(checkpoint.state, self.source, label=f"{label} state")
            _validate_digest(checkpoint.state_sha256, f"{label} state SHA-256")
            _validate_digest(
                checkpoint.source_sha256, f"{label} source SHA-256", allow_empty=True
            )
            if checkpoint.state_sha256 != checkpoint.state.sha256:
                raise ProjectValidationError(f"{label} state hash does not match.")
            if checkpoint.source_sha256 != expected_source_sha256:
                raise ProjectValidationError(f"{label} is bound to a different source capture.")
            checkpoint_bytes += checkpoint.state.serialized_size
        if checkpoint_bytes > MAX_CHECKPOINT_BYTES:
            raise ProjectValidationError(
                f"Project checkpoints exceed the {MAX_CHECKPOINT_BYTES}-byte limit."
            )

    @property
    def state_sha256(self) -> str:
        return project_state_sha256(self.tracks, self.metadata)

    def capture_state(self) -> ProjectState:
        return ProjectState.capture(self.tracks, self.metadata)

    def apply_state(self, state: ProjectState) -> None:
        """Replace editable state with an exact validated snapshot."""

        _validate_project_state(state, self.source, label="Restored project state")
        self.tracks = [Track.from_dict(asdict(track)) for track in state.tracks]
        self.metadata = dict(state.metadata)

    def append_history(
        self,
        *,
        action: str,
        summary: str,
        before: ProjectState,
        after: ProjectState | None = None,
        timestamp: str | None = None,
    ) -> EditHistoryEntry:
        """Append one exact edit transition and trim only the oldest entries."""

        after = after or self.capture_state()
        sequence = self.edit_history[-1].sequence + 1 if self.edit_history else 1
        entry = EditHistoryEntry.create(
            sequence=sequence,
            action=action,
            summary=summary,
            before=before,
            after=after,
            source_sha256=self.source.sha256,
            timestamp=timestamp,
        )
        original = list(self.edit_history)
        self.edit_history.append(entry)
        while len(self.edit_history) > MAX_EDIT_HISTORY:
            self.edit_history.pop(0)
        while (
            len(self.edit_history) > 1
            and sum(
                item.before.serialized_size + item.after.serialized_size
                for item in self.edit_history
            )
            > MAX_HISTORY_BYTES
        ):
            self.edit_history.pop(0)
        try:
            self.validate()
        except Exception:
            self.edit_history = original
            raise
        return entry

    def set_checkpoint(
        self, name: str, *, created_at: str | None = None
    ) -> ProjectCheckpoint:
        """Create or replace a named exact-state checkpoint."""

        name = _validate_checkpoint_name(name)
        checkpoint = ProjectCheckpoint.capture(
            name=name,
            project_revision=self.revision,
            tracks=self.tracks,
            metadata=self.metadata,
            source_sha256=self.source.sha256,
            created_at=created_at,
        )
        original = list(self.checkpoints)
        matching_index = next(
            (
                index
                for index, item in enumerate(self.checkpoints)
                if portable_name_key(item.name) == portable_name_key(name)
            ),
            None,
        )
        if matching_index is None:
            if len(self.checkpoints) >= MAX_CHECKPOINTS:
                raise ProjectValidationError(
                    f"A project cannot contain more than {MAX_CHECKPOINTS} checkpoints."
                )
            self.checkpoints.append(checkpoint)
        else:
            self.checkpoints[matching_index] = checkpoint
        try:
            self.validate()
        except Exception:
            self.checkpoints = original
            raise
        return checkpoint

    def remove_checkpoint(self, name: str) -> bool:
        name = _validate_checkpoint_name(name)
        for index, checkpoint in enumerate(self.checkpoints):
            if portable_name_key(checkpoint.name) == portable_name_key(name):
                del self.checkpoints[index]
                return True
        return False

    def checkpoint_state(self, name: str) -> ProjectState:
        name = _validate_checkpoint_name(name)
        for checkpoint in self.checkpoints:
            if portable_name_key(checkpoint.name) == portable_name_key(name):
                return ProjectState.capture(
                    checkpoint.state.tracks, checkpoint.state.metadata
                )
        raise ProjectValidationError(f"Checkpoint '{name}' was not found.")

    def touch(self) -> None:
        self.updated_at = utc_now_iso()


def _source_path_kind(value: str) -> str:
    """Classify a stored source path without touching the filesystem."""

    if "\x00" in value:
        raise ProjectValidationError("The source path contains a null byte.")
    windows = value.replace("/", "\\")
    folded = windows.casefold()
    if windows.startswith("\\\\"):
        raise ProjectValidationError(
            "UNC and Windows device-namespace source paths are not supported."
        )
    if folded.startswith(("\\??\\", "\\device\\", "\\global??\\")):
        raise ProjectValidationError(
            "Windows device-namespace source paths are not supported."
        )
    drive, tail = ntpath.splitdrive(value)
    if drive:
        if (
            len(drive) != 2
            or drive[1] != ":"
            or not drive[0].isalpha()
        ):
            raise ProjectValidationError(
                "UNC and Windows device-namespace source paths are not supported."
            )
        if not tail.startswith(("\\", "/")):
            raise ProjectValidationError(
                "Drive-relative source paths are not supported; use an absolute "
                "drive path or a project-relative path."
            )
        _reject_windows_device_components(tail)
        return "windows-absolute"
    if value.startswith("/") and os.name != "nt":
        return "native-absolute"
    if windows.startswith("\\"):
        raise ProjectValidationError(
            "Rooted Windows source paths require an explicit local drive."
        )
    _reject_windows_device_components(value)
    return "relative"


def _reject_windows_device_components(value: str) -> None:
    """Reject DOS devices and alternate streams in Windows-shaped paths."""

    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        "clock$",
        "conin$",
        "conout$",
    }
    reserved.update(f"com{suffix}" for suffix in "123456789¹²³")
    reserved.update(f"lpt{suffix}" for suffix in "123456789¹²³")
    for component in value.replace("/", "\\").split("\\"):
        if not component or component in {".", ".."}:
            continue
        normalized = component.rstrip(" .")
        stem = normalized.split(".", 1)[0].rstrip(" .").casefold()
        if stem in reserved:
            raise ProjectValidationError(
                "Windows device-name source paths are not supported."
            )
        if ":" in component:
            raise ProjectValidationError(
                "Windows alternate-stream source paths are not supported."
            )


def resolve_source_path(project: Project, project_path: Path) -> Path:
    source_path = project.source.path
    kind = _source_path_kind(source_path)
    _reject_windows_device_components(project.source.filename)
    stored = Path(source_path)
    candidates: list[Path] = []
    if kind in {"windows-absolute", "native-absolute"}:
        if kind != "windows-absolute" or os.name == "nt":
            candidates.append(stored)
    else:
        candidates.append((project_path.parent / stored).resolve())
    candidates.append((project_path.parent / project.source.filename).resolve())

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    rendered = "\n".join(f"  - {candidate}" for candidate in candidates)
    raise ProjectValidationError(
        "The source audio file could not be found. Checked:\n" + rendered
    )
