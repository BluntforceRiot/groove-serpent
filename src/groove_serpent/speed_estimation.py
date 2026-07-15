"""Deterministic, proposal-only constant-speed estimation from track durations.

The estimator deliberately does not decode audio, write project speed state, or
apply correction.  It compares exact integer project-track ranges with an
explicit reference track list, then uses robust statistics in log-ratio space.
It abstains unless duration independence is explicit and an exact current
receipt attests that every boundary received audio-and-visual review without
numerical fitting to those durations.  That receipt never approves correction.
Reference durations can describe a different edit/master, so every result is
evidence for owner review rather than approval.
"""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from statistics import median
from typing import Any, Literal, cast

from . import __version__
from .atomic_create import rename_no_replace
from .errors import ProjectValidationError
from .models import MAX_TRACKS, Project, resolve_source_path, utc_now_iso
from .project_io import load_project_with_sha256
from .publication import (
    canonical_json_sha256,
    capture_file_receipt,
    same_file_object_stats,
)
from .validation import strict_finite_number

REFERENCE_TRACKLIST_SCHEMA = "groove-serpent.speed-reference-tracklist/1"
DURATION_PROVENANCE_SCHEMA = "groove-serpent.speed-duration-provenance/1"
BOUNDARY_REVIEW_SCHEMA = "groove-serpent.speed-boundary-review/1"
SPEED_PROPOSAL_SCHEMA = "groove-serpent.speed-proposal/1"
SPEED_ALGORITHM = "track-duration-log-median/1"
FACTOR_DEFINITION = "reference_duration_seconds/observed_duration_seconds"
METHOD_NOTE = (
    "Equal-weight per-track reference/observed ratios; median and MAD in "
    "natural-log space; deterministic normal-approximation interval for the "
    "median with an uncertainty floor."
)
INTERPRETATION_NOTE = (
    "Reference durations may come from a different edit or master. The sampling "
    "interval covers only observed per-track ratio scatter, not edition, boundary, "
    "or reference-duration systematic uncertainty. This result requires boundary "
    "review, matched-loudness audition, and owner approval before any derivative "
    "correction."
)

MAX_REFERENCE_BYTES = 2 * 1024 * 1024
MAX_PROPOSAL_BYTES = 8 * 1024 * 1024
MAX_TEXT_LENGTH = 4_096
MAX_REFERENCE_DURATION_SECONDS = 24 * 60 * 60

_REFERENCE_ROOT_KEYS = {
    "schema",
    "artist",
    "album",
    "album_artist",
    "year",
    "genre",
    "side",
    "duration_provenance",
    "tracks",
}
_REFERENCE_METADATA_KEYS = _REFERENCE_ROOT_KEYS - {
    "schema",
    "duration_provenance",
    "tracks",
}
_REFERENCE_TRACK_REQUIRED_KEYS = {"title", "side", "duration"}
_REFERENCE_TRACK_OPTIONAL_KEYS = {"artist", "number"}
_DIGEST_CHARS = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class SpeedEstimatorConfig:
    """Version-one robust-estimator thresholds, included in every proposal."""

    minimum_usable_tracks: int = 4
    minimum_tracks_per_side: int = 2
    minimum_reference_duration_seconds: float = 45.0
    outlier_log_floor: float = 0.025
    outlier_mad_multiplier: float = 3.5
    maximum_robust_log_dispersion: float = 0.012
    maximum_side_log_delta: float = 0.015
    maximum_ci_log_half_width: float = 0.015
    uncertainty_floor_log: float = 0.0005
    supported_factor_minimum: float = 0.25
    supported_factor_maximum: float = 2.0

    def validate(self) -> None:
        if (
            type(self.minimum_usable_tracks) is not int
            or not 3 <= self.minimum_usable_tracks <= MAX_TRACKS
        ):
            raise ProjectValidationError(
                f"Minimum usable tracks must be an integer between 3 and {MAX_TRACKS}."
            )
        if (
            type(self.minimum_tracks_per_side) is not int
            or not 1 <= self.minimum_tracks_per_side <= self.minimum_usable_tracks
        ):
            raise ProjectValidationError(
                "Minimum tracks per side must be a positive integer no greater "
                "than minimum usable tracks."
            )
        numeric = {
            "minimum reference duration": self.minimum_reference_duration_seconds,
            "outlier log floor": self.outlier_log_floor,
            "outlier MAD multiplier": self.outlier_mad_multiplier,
            "maximum robust log dispersion": self.maximum_robust_log_dispersion,
            "maximum side log delta": self.maximum_side_log_delta,
            "maximum confidence-interval log half-width": (
                self.maximum_ci_log_half_width
            ),
            "uncertainty log floor": self.uncertainty_floor_log,
            "supported factor minimum": self.supported_factor_minimum,
            "supported factor maximum": self.supported_factor_maximum,
        }
        values = {
            label: strict_finite_number(value, f"Speed estimator {label}")
            for label, value in numeric.items()
        }
        if not 0 < values["minimum reference duration"] <= 3_600:
            raise ProjectValidationError(
                "Speed estimator minimum reference duration must be in (0, 3600]."
            )
        if not 0 <= values["outlier log floor"] <= 0.25:
            raise ProjectValidationError(
                "Speed estimator outlier log floor must be in [0, 0.25]."
            )
        if not 1 <= values["outlier MAD multiplier"] <= 10:
            raise ProjectValidationError(
                "Speed estimator outlier MAD multiplier must be in [1, 10]."
            )
        for label in (
            "maximum robust log dispersion",
            "maximum side log delta",
            "maximum confidence-interval log half-width",
            "uncertainty log floor",
        ):
            if not 0 < values[label] <= 0.25:
                raise ProjectValidationError(
                    f"Speed estimator {label} must be in (0, 0.25]."
                )
        if self.supported_factor_minimum != 0.25:
            raise ProjectValidationError(
                "Speed estimator factor minimum must remain the supported value 0.25."
            )
        if self.supported_factor_maximum != 2.0:
            raise ProjectValidationError(
                "Speed estimator factor maximum must remain the supported value 2.0."
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReferenceTrack:
    number: int
    title: str
    side: str
    duration_seconds: float
    artist: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReferenceDurationProvenance:
    source_description: str
    independent_of_project_boundaries: bool
    schema: str = DURATION_PROVENANCE_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SpeedReferenceTracklist:
    filename: str
    raw_sha256: str
    canonical_sha256: str
    metadata: dict[str, str]
    tracks: tuple[ReferenceTrack, ...]
    duration_provenance: ReferenceDurationProvenance | None = None

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema": REFERENCE_TRACKLIST_SCHEMA,
            "metadata": {key: self.metadata[key] for key in sorted(self.metadata)},
            "duration_provenance": (
                self.duration_provenance.to_dict()
                if self.duration_provenance is not None
                else None
            ),
            "tracks": [track.to_dict() for track in self.tracks],
        }


@dataclass(frozen=True, slots=True)
class BoundaryReviewEvidence:
    filename: str
    raw_sha256: str
    canonical_sha256: str
    project_sha256: str
    project_revision: int
    project_state_sha256: str
    source_sha256: str
    track_ranges_sha256: str
    reviewed_at: str
    review_method: str
    all_track_boundaries_reviewed: bool
    reviewed_boundaries_independent_of_reference_durations: bool
    correction_approval: str

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema": BOUNDARY_REVIEW_SCHEMA,
            "project_sha256": self.project_sha256,
            "project_revision": self.project_revision,
            "project_state_sha256": self.project_state_sha256,
            "source_sha256": self.source_sha256,
            "track_ranges_sha256": self.track_ranges_sha256,
            "reviewed_at": self.reviewed_at,
            "review_method": self.review_method,
            "all_track_boundaries_reviewed": self.all_track_boundaries_reviewed,
            "reviewed_boundaries_independent_of_reference_durations": (
                self.reviewed_boundaries_independent_of_reference_durations
            ),
            "correction_approval": self.correction_approval,
        }


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _is_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & flag)


def _stat_snapshot(value: os.stat_result) -> tuple[int | None, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        getattr(value, "st_birthtime_ns", None),
        getattr(value, "st_file_attributes", None),
    )


def _read_stable_plain_file(path: Path, maximum: int, label: str) -> bytes:
    path = _absolute_without_resolving(path)
    try:
        before = path.lstat()
        if path.is_symlink() or _is_reparse(before) or not stat.S_ISREG(before.st_mode):
            raise ProjectValidationError(
                f"{label} must be a regular, non-reparse file: {path.name}"
            )
        with path.open("rb") as handle:
            opened_before = os.fstat(handle.fileno())
            raw = handle.read(maximum + 1)
            opened_after = os.fstat(handle.fileno())
        after = path.lstat()
    except ProjectValidationError:
        raise
    except OSError as exc:
        raise ProjectValidationError(f"{label} could not be read: {exc}") from exc
    if len(raw) > maximum:
        raise ProjectValidationError(f"{label} exceeds the {maximum}-byte limit.")
    if (
        _stat_snapshot(opened_before) != _stat_snapshot(opened_after)
        or _stat_snapshot(before) != _stat_snapshot(after)
        or not same_file_object_stats(opened_after, after)
    ):
        raise ProjectValidationError(f"{label} changed while it was being read.")
    return raw


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Invalid JSON number: {value}")
    return result


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def _decode_json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (RecursionError, UnicodeDecodeError, ValueError) as exc:
        raise ProjectValidationError(f"{label} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProjectValidationError(f"{label} root must be a JSON object.")
    return value


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ProjectValidationError(f"{label} must be a JSON object with text keys.")
    return value


def _require_exact_keys(
    value: dict[str, Any],
    *,
    required: set[str],
    optional: set[str],
    label: str,
) -> None:
    missing = required - value.keys()
    if missing:
        raise ProjectValidationError(
            f"{label} is missing required field(s): {', '.join(sorted(missing))}."
        )
    unexpected = value.keys() - required - optional
    if unexpected:
        raise ProjectValidationError(
            f"{label} contains unexpected field(s): {', '.join(sorted(unexpected))}."
        )


def _bounded_text(
    value: Any, label: str, *, allow_empty: bool = False
) -> str:
    if not isinstance(value, str):
        raise ProjectValidationError(f"{label} must be text.")
    normalized = unicodedata.normalize("NFC", value.strip())
    if (
        (not normalized and not allow_empty)
        or len(normalized) > MAX_TEXT_LENGTH
        or any(ord(character) < 32 for character in normalized)
    ):
        qualifier = "0" if allow_empty else "1"
        raise ProjectValidationError(
            f"{label} must contain {qualifier}-{MAX_TEXT_LENGTH} printable characters."
        )
    return normalized


def _digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _DIGEST_CHARS for character in value)
    ):
        raise ProjectValidationError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _bounded_filename(value: Any, label: str) -> str:
    filename = _bounded_text(value, label)
    if Path(filename).name != filename:
        raise ProjectValidationError(f"{label} must not contain a directory path.")
    return filename


def load_speed_reference_tracklist(path: Path) -> SpeedReferenceTracklist:
    """Load one strict, duration-complete JSON reference track list."""

    absolute = _absolute_without_resolving(path)
    raw = _read_stable_plain_file(absolute, MAX_REFERENCE_BYTES, "Reference track list")
    data = _decode_json_object(raw, "Reference track list")
    _require_exact_keys(
        data,
        required={"artist", "album", "tracks"},
        optional=_REFERENCE_ROOT_KEYS - {"artist", "album", "tracks"},
        label="Reference track list",
    )
    if "schema" in data and data["schema"] != REFERENCE_TRACKLIST_SCHEMA:
        raise ProjectValidationError(
            f"Reference track-list schema must be '{REFERENCE_TRACKLIST_SCHEMA}'."
        )
    metadata: dict[str, str] = {}
    for key in sorted(_REFERENCE_METADATA_KEYS):
        if key in data:
            metadata[key] = _bounded_text(
                data[key], f"Reference track-list {key}", allow_empty=False
            )
    duration_provenance: ReferenceDurationProvenance | None = None
    raw_provenance = data.get("duration_provenance")
    if raw_provenance is not None:
        provenance = _require_mapping(
            raw_provenance, "Reference duration provenance"
        )
        _require_exact_keys(
            provenance,
            required={
                "schema",
                "source_description",
                "independent_of_project_boundaries",
            },
            optional=set(),
            label="Reference duration provenance",
        )
        if provenance["schema"] != DURATION_PROVENANCE_SCHEMA:
            raise ProjectValidationError(
                f"Reference duration provenance schema must be "
                f"'{DURATION_PROVENANCE_SCHEMA}'."
            )
        if type(provenance["independent_of_project_boundaries"]) is not bool:
            raise ProjectValidationError(
                "Reference duration independence must be an explicit boolean."
            )
        duration_provenance = ReferenceDurationProvenance(
            source_description=_bounded_text(
                provenance["source_description"],
                "Reference duration source description",
            ),
            independent_of_project_boundaries=provenance[
                "independent_of_project_boundaries"
            ],
        )
    raw_tracks = data["tracks"]
    if not isinstance(raw_tracks, list) or not 1 <= len(raw_tracks) <= MAX_TRACKS:
        raise ProjectValidationError(
            f"Reference track list must contain 1-{MAX_TRACKS} tracks."
        )
    tracks: list[ReferenceTrack] = []
    for index, item in enumerate(raw_tracks, start=1):
        track_data = _require_mapping(item, f"Reference track {index}")
        _require_exact_keys(
            track_data,
            required=_REFERENCE_TRACK_REQUIRED_KEYS,
            optional=_REFERENCE_TRACK_OPTIONAL_KEYS,
            label=f"Reference track {index}",
        )
        if "number" in track_data and (
            type(track_data["number"]) is not int or track_data["number"] != index
        ):
            raise ProjectValidationError(
                f"Reference track {index} number must be the consecutive integer {index}."
            )
        duration = strict_finite_number(
            track_data["duration"], f"Reference track {index} duration"
        )
        if not 0 < duration <= MAX_REFERENCE_DURATION_SECONDS:
            raise ProjectValidationError(
                f"Reference track {index} duration must be in (0, "
                f"{MAX_REFERENCE_DURATION_SECONDS}]."
            )
        tracks.append(
            ReferenceTrack(
                number=index,
                title=_bounded_text(track_data["title"], f"Reference track {index} title"),
                side=_bounded_text(track_data["side"], f"Reference track {index} side"),
                duration_seconds=duration,
                artist=_bounded_text(
                    track_data.get("artist", ""),
                    f"Reference track {index} artist",
                    allow_empty=True,
                ),
            )
        )
    provisional = SpeedReferenceTracklist(
        filename=absolute.name,
        raw_sha256=sha256(raw).hexdigest(),
        canonical_sha256="0" * 64,
        metadata=metadata,
        tracks=tuple(tracks),
        duration_provenance=duration_provenance,
    )
    return replace(
        provisional,
        canonical_sha256=canonical_json_sha256(provisional.canonical_dict()),
    )


def project_track_ranges_sha256(project: Project) -> str:
    """Hash exact current integer ranges without treating them as reviewed."""

    return canonical_json_sha256(
        [
            {
                "number": track.number,
                "start_sample": track.start_sample,
                "end_sample": track.end_sample,
            }
            for track in project.tracks
        ]
    )


def _review_timestamp(value: Any) -> str:
    text = _bounded_text(value, "Boundary review timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectValidationError(
            "Boundary review timestamp must be valid ISO-8601 text."
        ) from exc
    if parsed.tzinfo is None:
        raise ProjectValidationError(
            "Boundary review timestamp must include a timezone."
        )
    return text


def load_boundary_review_evidence(path: Path) -> BoundaryReviewEvidence:
    """Load an explicit review attestation; never create or infer one."""

    absolute = _absolute_without_resolving(path)
    raw = _read_stable_plain_file(
        absolute, MAX_REFERENCE_BYTES, "Boundary review evidence"
    )
    data = _decode_json_object(raw, "Boundary review evidence")
    expected = {
        "schema",
        "project_sha256",
        "project_revision",
        "project_state_sha256",
        "source_sha256",
        "track_ranges_sha256",
        "reviewed_at",
        "review_method",
        "all_track_boundaries_reviewed",
        "reviewed_boundaries_independent_of_reference_durations",
        "correction_approval",
    }
    _require_exact_keys(
        data,
        required=expected,
        optional=set(),
        label="Boundary review evidence",
    )
    if data["schema"] != BOUNDARY_REVIEW_SCHEMA:
        raise ProjectValidationError(
            f"Boundary review schema must be '{BOUNDARY_REVIEW_SCHEMA}'."
        )
    if type(data["project_revision"]) is not int or data["project_revision"] <= 0:
        raise ProjectValidationError(
            "Boundary review project revision must be a positive integer."
        )
    for key in (
        "all_track_boundaries_reviewed",
        "reviewed_boundaries_independent_of_reference_durations",
    ):
        if type(data[key]) is not bool:
            raise ProjectValidationError(
                f"Boundary review {key} must be an explicit boolean."
            )
    if data["review_method"] != "audio-and-visual-boundary-review":
        raise ProjectValidationError(
            "Boundary review method must be 'audio-and-visual-boundary-review'."
        )
    if data["correction_approval"] != "not-granted":
        raise ProjectValidationError(
            "Boundary review evidence cannot grant speed-correction approval."
        )
    evidence = BoundaryReviewEvidence(
        filename=absolute.name,
        raw_sha256=sha256(raw).hexdigest(),
        canonical_sha256="0" * 64,
        project_sha256=_digest(
            data["project_sha256"], "Boundary review project SHA-256"
        ),
        project_revision=data["project_revision"],
        project_state_sha256=_digest(
            data["project_state_sha256"],
            "Boundary review project-state SHA-256",
        ),
        source_sha256=_digest(
            data["source_sha256"], "Boundary review source SHA-256"
        ),
        track_ranges_sha256=_digest(
            data["track_ranges_sha256"], "Boundary review track-ranges SHA-256"
        ),
        reviewed_at=_review_timestamp(data["reviewed_at"]),
        review_method=data["review_method"],
        all_track_boundaries_reviewed=data["all_track_boundaries_reviewed"],
        reviewed_boundaries_independent_of_reference_durations=data[
            "reviewed_boundaries_independent_of_reference_durations"
        ],
        correction_approval=data["correction_approval"],
    )
    return replace(
        evidence,
        canonical_sha256=canonical_json_sha256(evidence.canonical_dict()),
    )


def _capture_current_source(project: Project, project_path: Path) -> None:
    source_path = resolve_source_path(project, project_path)
    receipt = capture_file_receipt(source_path, label="Speed-estimation source audio")
    if (
        receipt.sha256.lower() != project.source.sha256.lower()
        or receipt.size_bytes != project.source.size_bytes
    ):
        raise ProjectValidationError(
            "The live source audio does not match the project source identity; "
            "speed evidence was not created."
        )


def _boundary_review_status(
    project: Project,
    project_sha256: str,
    evidence: BoundaryReviewEvidence | None,
) -> Literal["missing", "stale", "inadequate", "current"]:
    if evidence is None:
        return "missing"
    exact_identity = (
        evidence.project_sha256 == project_sha256
        and evidence.project_revision == project.revision
        and evidence.project_state_sha256 == project.state_sha256
        and evidence.source_sha256 == project.source.sha256.lower()
        and evidence.track_ranges_sha256 == project_track_ranges_sha256(project)
    )
    if not exact_identity:
        return "stale"
    if (
        not evidence.all_track_boundaries_reviewed
        or not evidence.reviewed_boundaries_independent_of_reference_durations
    ):
        return "inadequate"
    return "current"


def _boundary_review_identity(
    evidence: BoundaryReviewEvidence | None,
    status: Literal["missing", "stale", "inadequate", "current"],
) -> dict[str, Any] | None:
    if evidence is None:
        return None
    return {
        "filename": evidence.filename,
        "raw_sha256": evidence.raw_sha256,
        "canonical_sha256": evidence.canonical_sha256,
        "schema": BOUNDARY_REVIEW_SCHEMA,
        "status": status,
        "reviewed_at": evidence.reviewed_at,
        "review_method": evidence.review_method,
        "project_sha256": evidence.project_sha256,
        "project_revision": evidence.project_revision,
        "project_state_sha256": evidence.project_state_sha256,
        "source_sha256": evidence.source_sha256,
        "track_ranges_sha256": evidence.track_ranges_sha256,
        "all_track_boundaries_reviewed": evidence.all_track_boundaries_reviewed,
        "reviewed_boundaries_independent_of_reference_durations": (
            evidence.reviewed_boundaries_independent_of_reference_durations
        ),
        "correction_approval": evidence.correction_approval,
    }


def _stable_float(value: float) -> float:
    if not math.isfinite(value):
        raise ProjectValidationError("Speed-estimation arithmetic became non-finite.")
    return float(format(value, ".15g"))


def _normalized_identity_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def _median_log(values: list[float]) -> float:
    if not values:
        raise ProjectValidationError("A speed center requires at least one ratio.")
    return float(median(values))


def _mad(values: list[float], center: float) -> float:
    return float(median([abs(value - center) for value in values]))


def _side_key(value: str) -> str:
    normalized = _normalized_identity_text(value)
    return normalized or "(unspecified)"


def _rpm_hypotheses(factor: float) -> list[dict[str, Any]]:
    """Return plausible decompositions without selecting nominal RPM state."""

    if not 0.25 <= factor <= 2.0:
        return []
    candidates: list[tuple[float, str, float | None, float | None, float]] = []
    same_fine = factor
    if 0.90 <= same_fine <= 1.10:
        candidates.append(
            (
                abs(math.log(same_fine)),
                "same-nominal-speed-unspecified",
                None,
                None,
                1.0,
            )
        )
    nominal_speeds = (100.0 / 6.0, 100.0 / 3.0, 45.0, 78.26)
    for capture_rpm in nominal_speeds:
        for intended_rpm in nominal_speeds:
            if math.isclose(capture_rpm, intended_rpm, rel_tol=0.0, abs_tol=1e-12):
                continue
            coarse = capture_rpm / intended_rpm
            if not 0.25 <= coarse <= 2.0:
                continue
            fine = factor / coarse
            if not 0.90 <= fine <= 1.10:
                continue
            label = (
                f"capture-{format(capture_rpm, '.12g')}-rpm/"
                f"intended-{format(intended_rpm, '.12g')}-rpm"
            )
            candidates.append(
                (abs(math.log(fine)), label, capture_rpm, intended_rpm, coarse)
            )
    candidates.sort(key=lambda item: (item[0], item[1]))
    result: list[dict[str, Any]] = []
    for rank, (
        _distance,
        label,
        candidate_capture_rpm,
        candidate_intended_rpm,
        coarse,
    ) in enumerate(
        candidates[:8], start=1
    ):
        fine = factor / coarse
        result.append(
            {
                "rank": rank,
                "nominal_pair": label,
                "capture_rpm": (
                    _stable_float(candidate_capture_rpm)
                    if candidate_capture_rpm is not None
                    else None
                ),
                "intended_rpm": (
                    _stable_float(candidate_intended_rpm)
                    if candidate_intended_rpm is not None
                    else None
                ),
                "coarse_factor": _stable_float(coarse),
                "fine_factor": _stable_float(fine),
                "reconstructed_factor": _stable_float(coarse * fine),
                "fine_delta_percent": _stable_float((fine - 1.0) * 100.0),
                "authority": "hypothesis-only-not-inferred",
            }
        )
    return result


def _tool_identity() -> dict[str, str]:
    module_bytes = _read_stable_plain_file(
        Path(__file__), MAX_PROPOSAL_BYTES, "Speed estimator implementation"
    )
    basis = {
        "name": "groove-serpent",
        "version": __version__,
        "algorithm": SPEED_ALGORITHM,
        "module_sha256": sha256(module_bytes).hexdigest(),
    }
    return {**basis, "sha256": canonical_json_sha256(basis)}


def _project_identity(project: Project, filename: str, project_sha256: str) -> dict[str, Any]:
    return {
        "filename": filename,
        "sha256": _digest(project_sha256, "Project SHA-256"),
        "schema_version": project.schema_version,
        "revision": project.revision,
        "state_sha256": project.state_sha256,
        "track_ranges_sha256": project_track_ranges_sha256(project),
    }


def _source_identity(project: Project) -> dict[str, Any]:
    source_sha256 = _digest(project.source.sha256.lower(), "Source SHA-256")
    return {
        "filename": project.source.filename,
        "sha256": source_sha256,
        "sample_rate": project.source.sample_rate,
        "sample_count": project.source.sample_count,
        "channels": project.source.channels,
    }


def _reference_identity(reference: SpeedReferenceTracklist) -> dict[str, Any]:
    provenance = reference.duration_provenance
    if provenance is None:
        independence_status = "unconfirmed"
        provenance_sha256 = None
    else:
        independence_status = (
            "independent"
            if provenance.independent_of_project_boundaries
            else "not-independent"
        )
        provenance_sha256 = canonical_json_sha256(provenance.to_dict())
    return {
        "filename": reference.filename,
        "raw_sha256": reference.raw_sha256,
        "canonical_sha256": reference.canonical_sha256,
        "schema": REFERENCE_TRACKLIST_SCHEMA,
        "track_count": len(reference.tracks),
        "duration_independence_status": independence_status,
        "duration_provenance_sha256": provenance_sha256,
        "duration_provenance": (
            provenance.to_dict() if provenance is not None else None
        ),
    }


def _release_identity_status(
    project: Project, reference: SpeedReferenceTracklist
) -> Literal["matched", "mismatch", "project-metadata-missing"]:
    reference_artist = reference.metadata["artist"]
    reference_album = reference.metadata["album"]
    project_artist = project.metadata.get("artist") or project.metadata.get(
        "album_artist"
    )
    project_album = project.metadata.get("album")
    if not project_artist or not project_album:
        return "project-metadata-missing"
    if (
        _normalized_identity_text(project_artist)
        != _normalized_identity_text(reference_artist)
        or _normalized_identity_text(project_album)
        != _normalized_identity_text(reference_album)
    ):
        return "mismatch"
    return "matched"


def _track_evidence(
    project: Project,
    reference: SpeedReferenceTracklist,
    config: SpeedEstimatorConfig,
) -> tuple[list[dict[str, Any]], list[str]]:
    if len(project.tracks) != len(reference.tracks):
        return [], ["track_count_mismatch"]
    rows: list[dict[str, Any]] = []
    identity_mismatch = False
    for project_track, reference_track in zip(
        project.tracks, reference.tracks, strict=True
    ):
        title_match = _normalized_identity_text(project_track.title) == (
            _normalized_identity_text(reference_track.title)
        )
        side_match = _side_key(project_track.side) == _side_key(reference_track.side)
        observed = (
            project_track.end_sample - project_track.start_sample
        ) / project.source.sample_rate
        disposition: Literal["candidate", "excluded"] = "candidate"
        exclusion_reason: str | None = None
        if project_track.number != reference_track.number:
            disposition = "excluded"
            exclusion_reason = "track_number_mismatch"
            identity_mismatch = True
        elif not title_match:
            disposition = "excluded"
            exclusion_reason = "track_title_mismatch"
            identity_mismatch = True
        elif not side_match:
            disposition = "excluded"
            exclusion_reason = "track_side_mismatch"
            identity_mismatch = True
        elif reference_track.duration_seconds < config.minimum_reference_duration_seconds:
            disposition = "excluded"
            exclusion_reason = "reference_duration_too_short"
        elif observed <= 0:
            disposition = "excluded"
            exclusion_reason = "observed_duration_not_positive"
        ratio: float | None = None
        log_ratio: float | None = None
        if disposition == "candidate":
            ratio = reference_track.duration_seconds / observed
            log_ratio = math.log(ratio)
        rows.append(
            {
                "project_track_number": project_track.number,
                "reference_track_number": reference_track.number,
                "project_title": project_track.title,
                "reference_title": reference_track.title,
                "project_side": project_track.side,
                "reference_side": reference_track.side,
                "start_sample": project_track.start_sample,
                "end_sample": project_track.end_sample,
                "observed_duration_seconds": _stable_float(observed),
                "reference_duration_seconds": _stable_float(
                    reference_track.duration_seconds
                ),
                "raw_factor": _stable_float(ratio) if ratio is not None else None,
                "log_factor": (
                    _stable_float(log_ratio) if log_ratio is not None else None
                ),
                "disposition": disposition,
                "exclusion_reason": exclusion_reason,
            }
        )
    reasons = ["reference_identity_mismatch"] if identity_mismatch else []
    return rows, reasons


def _apply_outlier_rule(
    rows: list[dict[str, Any]], config: SpeedEstimatorConfig
) -> None:
    candidates = [
        cast(float, row["log_factor"])
        for row in rows
        if row["disposition"] == "candidate"
    ]
    if len(candidates) < 3:
        return
    center = _median_log(candidates)
    robust_sigma = 1.4826 * _mad(candidates, center)
    threshold = max(
        config.outlier_log_floor,
        config.outlier_mad_multiplier * robust_sigma,
    )
    for row in rows:
        value = row["log_factor"]
        if row["disposition"] == "candidate" and isinstance(value, float):
            if abs(value - center) > threshold:
                row["disposition"] = "excluded"
                row["exclusion_reason"] = "robust_log_outlier"


def _summarize_ratios(
    rows: list[dict[str, Any]], config: SpeedEstimatorConfig
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    usable_rows = [row for row in rows if row["disposition"] == "candidate"]
    reasons: list[str] = []
    if len(usable_rows) < config.minimum_usable_tracks:
        reasons.append("insufficient_usable_tracks")
    logs = [cast(float, row["log_factor"]) for row in usable_rows]
    if not logs:
        empty: dict[str, Any] = {
            "status": "abstained",
            "confidence": "none",
            "factor_definition": FACTOR_DEFINITION,
            "proposed_factor": None,
            "diagnostic_center_factor": None,
            "sampling_confidence_interval_95": None,
            "robust_log_dispersion": None,
            "relative_dispersion": None,
            "rpm_hypothesis_status": "hypotheses-only-not-inferred",
            "rpm_hypotheses": [],
        }
        return empty, [], reasons

    center_log = _median_log(logs)
    center_factor = math.exp(center_log)
    robust_log_dispersion = 1.4826 * _mad(logs, center_log)
    standard_error = 1.2533 * max(
        robust_log_dispersion, config.uncertainty_floor_log
    ) / math.sqrt(len(logs))
    half_width = 1.96 * standard_error
    lower = math.exp(center_log - half_width)
    upper = math.exp(center_log + half_width)
    if not config.supported_factor_minimum <= center_factor <= (
        config.supported_factor_maximum
    ):
        reasons.append("factor_outside_supported_range")
    if robust_log_dispersion > config.maximum_robust_log_dispersion:
        reasons.append("track_ratios_inconsistent")
    if half_width > config.maximum_ci_log_half_width:
        reasons.append("confidence_interval_too_wide")

    by_side: dict[str, list[float]] = {}
    display_side: dict[str, str] = {}
    for row in usable_rows:
        raw_side = cast(str, row["reference_side"])
        key = _side_key(raw_side)
        by_side.setdefault(key, []).append(cast(float, row["log_factor"]))
        display_side.setdefault(key, raw_side or "(unspecified)")
    side_summaries: list[dict[str, Any]] = []
    side_centers: list[float] = []
    for key in sorted(by_side):
        side_logs = by_side[key]
        side_center = _median_log(side_logs)
        side_dispersion = 1.4826 * _mad(side_logs, side_center)
        side_centers.append(side_center)
        side_summaries.append(
            {
                "side": display_side[key],
                "usable_track_count": len(side_logs),
                "center_factor": _stable_float(math.exp(side_center)),
                "robust_log_dispersion": _stable_float(side_dispersion),
            }
        )
    if len(by_side) >= 2:
        if any(
            len(side_logs) < config.minimum_tracks_per_side
            for side_logs in by_side.values()
        ):
            reasons.append("insufficient_independent_tracks_per_side")
        if max(side_centers) - min(side_centers) > config.maximum_side_log_delta:
            reasons.append("side_estimates_disagree")

    confidence: Literal["high", "medium", "low"]
    if (
        len(logs) >= 6
        and len(by_side) >= 2
        and robust_log_dispersion <= 0.003
        and half_width <= 0.004
    ):
        confidence = "high"
    elif (
        len(logs) >= config.minimum_usable_tracks
        and robust_log_dispersion <= 0.01
        and half_width <= 0.015
    ):
        confidence = "medium"
    else:
        confidence = "low"
        reasons.append("confidence_below_proposal_threshold")

    reasons = list(dict.fromkeys(reasons))
    abstained = bool(reasons)
    estimate: dict[str, Any] = {
        "status": "abstained" if abstained else "proposed",
        "confidence": "none" if abstained else confidence,
        "factor_definition": FACTOR_DEFINITION,
        "proposed_factor": None if abstained else _stable_float(center_factor),
        "diagnostic_center_factor": _stable_float(center_factor),
        "sampling_confidence_interval_95": [
            _stable_float(lower),
            _stable_float(upper),
        ],
        "robust_log_dispersion": _stable_float(robust_log_dispersion),
        "relative_dispersion": _stable_float(
            math.expm1(robust_log_dispersion)
        ),
        "rpm_hypothesis_status": "hypotheses-only-not-inferred",
        "rpm_hypotheses": _rpm_hypotheses(center_factor),
    }
    return estimate, side_summaries, reasons


def estimate_speed_project(
    project: Project,
    *,
    project_filename: str,
    project_sha256: str,
    reference: SpeedReferenceTracklist,
    boundary_review: BoundaryReviewEvidence | None = None,
    config: SpeedEstimatorConfig | None = None,
) -> dict[str, Any]:
    """Create one sealed proposal from already-loaded, unchanged inputs."""

    selected_config = config or SpeedEstimatorConfig()
    selected_config.validate()
    project.validate()
    rows, reasons = _track_evidence(project, reference, selected_config)
    release_identity_status = _release_identity_status(project, reference)
    if release_identity_status == "mismatch":
        reasons.insert(0, "release_metadata_mismatch")
    elif release_identity_status == "project-metadata-missing":
        reasons.insert(0, "project_release_metadata_missing")
    boundary_review_status = _boundary_review_status(
        project, project_sha256, boundary_review
    )
    provenance = reference.duration_provenance
    duration_independence_status = (
        "unconfirmed"
        if provenance is None
        else (
            "independent"
            if provenance.independent_of_project_boundaries
            else "not-independent"
        )
    )
    safety_reasons: list[str] = []
    if duration_independence_status == "unconfirmed":
        safety_reasons.append("reference_duration_independence_unconfirmed")
    elif duration_independence_status == "not-independent":
        safety_reasons.append("reference_durations_not_independent")
    if boundary_review_status == "missing":
        safety_reasons.append("boundary_review_evidence_missing")
    elif boundary_review_status == "stale":
        safety_reasons.append("boundary_review_evidence_stale")
    elif boundary_review_status == "inadequate":
        safety_reasons.append("boundary_review_evidence_inadequate")
    reasons = safety_reasons + reasons
    if rows and "reference_identity_mismatch" not in reasons:
        _apply_outlier_rule(rows, selected_config)
    estimate, side_summaries, statistical_reasons = _summarize_ratios(
        rows, selected_config
    )
    reasons.extend(statistical_reasons)
    reasons = list(dict.fromkeys(reasons))
    if reasons:
        estimate["status"] = "abstained"
        estimate["confidence"] = "none"
        estimate["proposed_factor"] = None

    config_values = selected_config.to_dict()
    payload: dict[str, Any] = {
        "schema": SPEED_PROPOSAL_SCHEMA,
        "authority": {
            "mode": "proposal-only",
            "may_apply_correction": False,
            "may_change_project": False,
            "human_approval": "not-inferred",
            "boundary_review": "owner-review-required-not-inferred",
        },
        "project": _project_identity(project, project_filename, project_sha256),
        "source": _source_identity(project),
        "reference_tracklist": _reference_identity(reference),
        "boundary_review_evidence": _boundary_review_identity(
            boundary_review, boundary_review_status
        ),
        "tool": _tool_identity(),
        "config": {
            "values": config_values,
            "sha256": canonical_json_sha256(config_values),
        },
        "tracks": rows,
        "side_summaries": side_summaries,
        "diagnostics": {
            "project_track_count": len(project.tracks),
            "reference_track_count": len(reference.tracks),
            "usable_track_count": sum(
                row["disposition"] == "candidate" for row in rows
            ),
            "excluded_track_count": sum(
                row["disposition"] == "excluded" for row in rows
            ),
            "independent_side_count": len(side_summaries),
            "release_identity_status": release_identity_status,
            "reference_duration_independence_status": (
                duration_independence_status
            ),
            "boundary_review_status": boundary_review_status,
            "abstention_reasons": reasons,
            "method_note": METHOD_NOTE,
            "interpretation_note": INTERPRETATION_NOTE,
        },
        "estimate": estimate,
    }
    payload["proposal_sha256"] = canonical_json_sha256(payload)
    validate_speed_proposal(payload)
    return payload


def estimate_speed(
    project_path: Path,
    tracklist_path: Path,
    *,
    boundary_review_path: Path | None = None,
    config: SpeedEstimatorConfig | None = None,
) -> dict[str, Any]:
    """Load exact current inputs and return a proposal without writing either."""

    absolute_project = _absolute_without_resolving(project_path)
    project, project_sha256 = load_project_with_sha256(absolute_project)
    _capture_current_source(project, absolute_project)
    reference = load_speed_reference_tracklist(tracklist_path)
    boundary_review = (
        load_boundary_review_evidence(boundary_review_path)
        if boundary_review_path is not None
        else None
    )
    repeated_project, repeated_sha256 = load_project_with_sha256(absolute_project)
    if (
        repeated_sha256 != project_sha256
        or repeated_project.state_sha256 != project.state_sha256
    ):
        raise ProjectValidationError(
            "The project changed while speed evidence was being gathered."
        )
    return estimate_speed_project(
        project,
        project_filename=absolute_project.name,
        project_sha256=project_sha256,
        reference=reference,
        boundary_review=boundary_review,
        config=config,
    )


def _validate_identity_objects(data: dict[str, Any]) -> None:
    project = _require_mapping(data["project"], "Speed proposal project identity")
    _require_exact_keys(
        project,
        required={
            "filename",
            "sha256",
            "schema_version",
            "revision",
            "state_sha256",
            "track_ranges_sha256",
        },
        optional=set(),
        label="Speed proposal project identity",
    )
    _bounded_filename(project["filename"], "Speed proposal project filename")
    _digest(project["sha256"], "Speed proposal project SHA-256")
    _digest(project["state_sha256"], "Speed proposal project state SHA-256")
    _digest(
        project["track_ranges_sha256"],
        "Speed proposal project track-ranges SHA-256",
    )
    if type(project["schema_version"]) is not int or project["schema_version"] <= 0:
        raise ProjectValidationError("Speed proposal project schema must be positive.")
    if type(project["revision"]) is not int or project["revision"] <= 0:
        raise ProjectValidationError("Speed proposal project revision must be positive.")

    source = _require_mapping(data["source"], "Speed proposal source identity")
    _require_exact_keys(
        source,
        required={"filename", "sha256", "sample_rate", "sample_count", "channels"},
        optional=set(),
        label="Speed proposal source identity",
    )
    _bounded_filename(source["filename"], "Speed proposal source filename")
    _digest(source["sha256"], "Speed proposal source SHA-256")
    for key in ("sample_rate", "channels"):
        if type(source[key]) is not int or source[key] <= 0:
            raise ProjectValidationError(
                f"Speed proposal source {key} must be a positive integer."
            )
    if source["sample_count"] is not None and (
        type(source["sample_count"]) is not int or source["sample_count"] <= 0
    ):
        raise ProjectValidationError(
            "Speed proposal source sample_count must be null or a positive integer."
        )

    reference = _require_mapping(
        data["reference_tracklist"], "Speed proposal reference identity"
    )
    _require_exact_keys(
        reference,
        required={
            "filename",
            "raw_sha256",
            "canonical_sha256",
            "schema",
            "track_count",
            "duration_independence_status",
            "duration_provenance_sha256",
            "duration_provenance",
        },
        optional=set(),
        label="Speed proposal reference identity",
    )
    _bounded_filename(reference["filename"], "Speed proposal reference filename")
    _digest(reference["raw_sha256"], "Speed proposal reference raw SHA-256")
    _digest(
        reference["canonical_sha256"], "Speed proposal reference canonical SHA-256"
    )
    if reference["schema"] != REFERENCE_TRACKLIST_SCHEMA:
        raise ProjectValidationError("Speed proposal reference schema is unsupported.")
    if type(reference["track_count"]) is not int or not 1 <= reference[
        "track_count"
    ] <= MAX_TRACKS:
        raise ProjectValidationError("Speed proposal reference track count is invalid.")
    if reference["duration_independence_status"] not in {
        "unconfirmed",
        "independent",
        "not-independent",
    }:
        raise ProjectValidationError(
            "Speed proposal reference duration independence is unsupported."
        )
    provenance_sha256 = reference["duration_provenance_sha256"]
    provenance_data = reference["duration_provenance"]
    if reference["duration_independence_status"] == "unconfirmed":
        if provenance_sha256 is not None or provenance_data is not None:
            raise ProjectValidationError(
                "Unconfirmed duration provenance cannot contain provenance evidence."
            )
    else:
        _digest(provenance_sha256, "Speed proposal duration provenance SHA-256")
        provenance = _require_mapping(
            provenance_data, "Speed proposal duration provenance"
        )
        _require_exact_keys(
            provenance,
            required={
                "schema",
                "source_description",
                "independent_of_project_boundaries",
            },
            optional=set(),
            label="Speed proposal duration provenance",
        )
        if provenance["schema"] != DURATION_PROVENANCE_SCHEMA:
            raise ProjectValidationError(
                "Speed proposal duration provenance schema is unsupported."
            )
        _bounded_text(
            provenance["source_description"],
            "Speed proposal duration provenance description",
        )
        if type(provenance["independent_of_project_boundaries"]) is not bool:
            raise ProjectValidationError(
                "Speed proposal duration provenance independence must be boolean."
            )
        expected_independent = reference["duration_independence_status"] == "independent"
        if provenance["independent_of_project_boundaries"] != expected_independent:
            raise ProjectValidationError(
                "Speed proposal duration provenance status does not match."
            )
        if provenance_sha256 != canonical_json_sha256(provenance):
            raise ProjectValidationError(
                "Speed proposal duration provenance hash does not match."
            )

    boundary = data["boundary_review_evidence"]
    if boundary is None:
        return
    boundary_data = _require_mapping(
        boundary, "Speed proposal boundary-review identity"
    )
    _require_exact_keys(
        boundary_data,
        required={
            "filename",
            "raw_sha256",
            "canonical_sha256",
            "schema",
            "status",
            "reviewed_at",
            "review_method",
            "project_sha256",
            "project_revision",
            "project_state_sha256",
            "source_sha256",
            "track_ranges_sha256",
            "all_track_boundaries_reviewed",
            "reviewed_boundaries_independent_of_reference_durations",
            "correction_approval",
        },
        optional=set(),
        label="Speed proposal boundary-review identity",
    )
    _bounded_filename(
        boundary_data["filename"], "Speed proposal boundary-review filename"
    )
    _digest(
        boundary_data["raw_sha256"], "Speed proposal boundary-review raw SHA-256"
    )
    _digest(
        boundary_data["canonical_sha256"],
        "Speed proposal boundary-review canonical SHA-256",
    )
    if boundary_data["schema"] != BOUNDARY_REVIEW_SCHEMA:
        raise ProjectValidationError(
            "Speed proposal boundary-review schema is unsupported."
        )
    if boundary_data["status"] not in {"stale", "inadequate", "current"}:
        raise ProjectValidationError(
            "Speed proposal boundary-review status is unsupported."
        )
    _review_timestamp(boundary_data["reviewed_at"])
    if boundary_data["review_method"] != "audio-and-visual-boundary-review":
        raise ProjectValidationError(
            "Speed proposal boundary-review method is unsupported."
        )
    if boundary_data["correction_approval"] != "not-granted":
        raise ProjectValidationError(
            "Speed proposal boundary review cannot grant correction approval."
        )
    for key in (
        "project_sha256",
        "project_state_sha256",
        "source_sha256",
        "track_ranges_sha256",
    ):
        _digest(
            boundary_data[key],
            f"Speed proposal boundary-review {key}",
        )
    if (
        type(boundary_data["project_revision"]) is not int
        or boundary_data["project_revision"] <= 0
    ):
        raise ProjectValidationError(
            "Speed proposal boundary-review revision must be positive."
        )
    for key in (
        "all_track_boundaries_reviewed",
        "reviewed_boundaries_independent_of_reference_durations",
    ):
        if type(boundary_data[key]) is not bool:
            raise ProjectValidationError(
                f"Speed proposal boundary-review {key} must be boolean."
            )
    canonical_receipt = {
        "schema": boundary_data["schema"],
        "project_sha256": boundary_data["project_sha256"],
        "project_revision": boundary_data["project_revision"],
        "project_state_sha256": boundary_data["project_state_sha256"],
        "source_sha256": boundary_data["source_sha256"],
        "track_ranges_sha256": boundary_data["track_ranges_sha256"],
        "reviewed_at": boundary_data["reviewed_at"],
        "review_method": boundary_data["review_method"],
        "all_track_boundaries_reviewed": boundary_data[
            "all_track_boundaries_reviewed"
        ],
        "reviewed_boundaries_independent_of_reference_durations": boundary_data[
            "reviewed_boundaries_independent_of_reference_durations"
        ],
        "correction_approval": boundary_data["correction_approval"],
    }
    if boundary_data["canonical_sha256"] != canonical_json_sha256(
        canonical_receipt
    ):
        raise ProjectValidationError(
            "Speed proposal boundary-review canonical hash does not match."
        )


def _validate_tool_and_config(data: dict[str, Any]) -> None:
    tool = _require_mapping(data["tool"], "Speed proposal tool identity")
    _require_exact_keys(
        tool,
        required={"name", "version", "algorithm", "module_sha256", "sha256"},
        optional=set(),
        label="Speed proposal tool identity",
    )
    basis = {
        key: tool[key]
        for key in ("name", "version", "algorithm", "module_sha256")
    }
    for key, value in basis.items():
        if key == "module_sha256":
            _digest(value, "Speed proposal tool module SHA-256")
            continue
        _bounded_text(value, f"Speed proposal tool {key}")
    _digest(tool["sha256"], "Speed proposal tool SHA-256")
    if tool["sha256"] != canonical_json_sha256(basis):
        raise ProjectValidationError("Speed proposal tool identity hash does not match.")

    config = _require_mapping(data["config"], "Speed proposal config identity")
    _require_exact_keys(
        config,
        required={"values", "sha256"},
        optional=set(),
        label="Speed proposal config identity",
    )
    values = _require_mapping(config["values"], "Speed proposal config values")
    expected_config_keys = set(SpeedEstimatorConfig.__dataclass_fields__)
    _require_exact_keys(
        values,
        required=expected_config_keys,
        optional=set(),
        label="Speed proposal config values",
    )
    selected = SpeedEstimatorConfig(**values)
    selected.validate()
    _digest(config["sha256"], "Speed proposal config SHA-256")
    if config["sha256"] != canonical_json_sha256(values):
        raise ProjectValidationError("Speed proposal config hash does not match.")


def _validate_proposal_rows(data: dict[str, Any]) -> None:
    tracks = data["tracks"]
    if not isinstance(tracks, list) or len(tracks) > MAX_TRACKS:
        raise ProjectValidationError("Speed proposal tracks must be a bounded array.")
    source = cast(dict[str, Any], data["source"])
    source_rate = cast(int, source["sample_rate"])
    config_values = cast(dict[str, Any], cast(dict[str, Any], data["config"])["values"])
    config = SpeedEstimatorConfig(**config_values)
    previous_end: int | None = None
    track_keys = {
        "project_track_number",
        "reference_track_number",
        "project_title",
        "reference_title",
        "project_side",
        "reference_side",
        "start_sample",
        "end_sample",
        "observed_duration_seconds",
        "reference_duration_seconds",
        "raw_factor",
        "log_factor",
        "disposition",
        "exclusion_reason",
    }
    for index, item in enumerate(tracks, start=1):
        row = _require_mapping(item, f"Speed proposal track {index}")
        _require_exact_keys(
            row,
            required=track_keys,
            optional=set(),
            label=f"Speed proposal track {index}",
        )
        if (
            type(row["project_track_number"]) is not int
            or row["project_track_number"] != index
            or type(row["reference_track_number"]) is not int
            or row["reference_track_number"] != index
        ):
            raise ProjectValidationError(
                f"Speed proposal track {index} numbers are invalid."
            )
        for key in (
            "project_title",
            "reference_title",
            "reference_side",
        ):
            _bounded_text(row[key], f"Speed proposal track {index} {key}")
        _bounded_text(
            row["project_side"],
            f"Speed proposal track {index} project_side",
            allow_empty=True,
        )
        for key in ("start_sample", "end_sample"):
            if type(row[key]) is not int or row[key] < 0:
                raise ProjectValidationError(
                    f"Speed proposal track {index} {key} must be non-negative."
                )
        start_sample = cast(int, row["start_sample"])
        end_sample = cast(int, row["end_sample"])
        if end_sample <= start_sample:
            raise ProjectValidationError(
                f"Speed proposal track {index} must have a positive sample range."
            )
        if previous_end is not None and start_sample != previous_end:
            raise ProjectValidationError(
                "Speed proposal track ranges must remain exactly contiguous."
            )
        previous_end = end_sample
        for key in ("observed_duration_seconds", "reference_duration_seconds"):
            if strict_finite_number(
                row[key], f"Speed proposal track {index} {key}"
            ) <= 0:
                raise ProjectValidationError(
                    f"Speed proposal track {index} {key} must be positive."
                )
        observed = cast(float, row["observed_duration_seconds"])
        reference_duration = cast(float, row["reference_duration_seconds"])
        exact_observed = (end_sample - start_sample) / source_rate
        if not math.isclose(observed, exact_observed, rel_tol=1e-13, abs_tol=1e-13):
            raise ProjectValidationError(
                f"Speed proposal track {index} observed duration disagrees "
                "with its exact sample range."
            )
        expected_identity_exclusion: str | None = None
        if _normalized_identity_text(cast(str, row["project_title"])) != (
            _normalized_identity_text(cast(str, row["reference_title"]))
        ):
            expected_identity_exclusion = "track_title_mismatch"
        elif _side_key(cast(str, row["project_side"])) != _side_key(
            cast(str, row["reference_side"])
        ):
            expected_identity_exclusion = "track_side_mismatch"
        elif reference_duration < config.minimum_reference_duration_seconds:
            expected_identity_exclusion = "reference_duration_too_short"
        if row["disposition"] not in {"candidate", "excluded"}:
            raise ProjectValidationError(
                f"Speed proposal track {index} disposition is unsupported."
            )
        if expected_identity_exclusion is not None:
            if (
                row["disposition"] != "excluded"
                or row["exclusion_reason"] != expected_identity_exclusion
                or row["raw_factor"] is not None
                or row["log_factor"] is not None
            ):
                raise ProjectValidationError(
                    f"Speed proposal track {index} identity/duration exclusion "
                    "does not match its evidence."
                )
        else:
            if row["disposition"] == "excluded" and row[
                "exclusion_reason"
            ] != "robust_log_outlier":
                raise ProjectValidationError(
                    f"Speed proposal track {index} has an unsupported exclusion."
                )
            if row["disposition"] == "candidate" and row["exclusion_reason"] is not None:
                raise ProjectValidationError(
                    f"Speed proposal track {index} candidate cannot have an exclusion."
                )
            raw_factor = strict_finite_number(
                row["raw_factor"], f"Speed proposal track {index} raw_factor"
            )
            log_factor = strict_finite_number(
                row["log_factor"], f"Speed proposal track {index} log_factor"
            )
            expected_factor = reference_duration / exact_observed
            if raw_factor <= 0 or not math.isclose(
                raw_factor, expected_factor, rel_tol=1e-13, abs_tol=1e-13
            ):
                raise ProjectValidationError(
                    f"Speed proposal track {index} factor disagrees with its durations."
                )
            if not math.isclose(
                log_factor, math.log(raw_factor), rel_tol=1e-13, abs_tol=1e-13
            ):
                raise ProjectValidationError(
                    f"Speed proposal track {index} log factor does not match."
                )
        if row["disposition"] == "candidate":
            if row["exclusion_reason"] is not None:
                raise ProjectValidationError(
                    f"Speed proposal track {index} candidate cannot have an exclusion."
                )
        else:
            _bounded_text(
                row["exclusion_reason"],
                f"Speed proposal track {index} exclusion reason",
            )

    sides = data["side_summaries"]
    if not isinstance(sides, list) or len(sides) > MAX_TRACKS:
        raise ProjectValidationError(
            "Speed proposal side summaries must be a bounded array."
        )
    for index, item in enumerate(sides, start=1):
        side = _require_mapping(item, f"Speed proposal side summary {index}")
        _require_exact_keys(
            side,
            required={
                "side",
                "usable_track_count",
                "center_factor",
                "robust_log_dispersion",
            },
            optional=set(),
            label=f"Speed proposal side summary {index}",
        )
        _bounded_text(side["side"], f"Speed proposal side summary {index} side")
        if type(side["usable_track_count"]) is not int or side[
            "usable_track_count"
        ] <= 0:
            raise ProjectValidationError(
                f"Speed proposal side summary {index} count must be positive."
            )
        strict_finite_number(
            side["center_factor"], f"Speed proposal side summary {index} center"
        )
        strict_finite_number(
            side["robust_log_dispersion"],
            f"Speed proposal side summary {index} dispersion",
        )


def _validate_diagnostics_and_estimate(data: dict[str, Any]) -> None:
    diagnostics = _require_mapping(data["diagnostics"], "Speed proposal diagnostics")
    _require_exact_keys(
        diagnostics,
        required={
            "project_track_count",
            "reference_track_count",
            "usable_track_count",
            "excluded_track_count",
            "independent_side_count",
            "release_identity_status",
            "reference_duration_independence_status",
            "boundary_review_status",
            "abstention_reasons",
            "method_note",
            "interpretation_note",
        },
        optional=set(),
        label="Speed proposal diagnostics",
    )
    for key in (
        "project_track_count",
        "reference_track_count",
        "usable_track_count",
        "excluded_track_count",
        "independent_side_count",
    ):
        if type(diagnostics[key]) is not int or diagnostics[key] < 0:
            raise ProjectValidationError(
                f"Speed proposal diagnostic {key} must be non-negative."
            )
    if diagnostics["release_identity_status"] not in {
        "matched",
        "mismatch",
        "project-metadata-missing",
    }:
        raise ProjectValidationError(
            "Speed proposal release identity status is unsupported."
        )
    if diagnostics["reference_duration_independence_status"] not in {
        "unconfirmed",
        "independent",
        "not-independent",
    }:
        raise ProjectValidationError(
            "Speed proposal duration independence status is unsupported."
        )
    if diagnostics["boundary_review_status"] not in {
        "missing",
        "stale",
        "inadequate",
        "current",
    }:
        raise ProjectValidationError(
            "Speed proposal boundary review status is unsupported."
        )
    reasons = diagnostics["abstention_reasons"]
    if not isinstance(reasons, list) or len(reasons) > MAX_TRACKS:
        raise ProjectValidationError(
            "Speed proposal abstention reasons must be a bounded array."
        )
    for index, reason in enumerate(reasons, start=1):
        _bounded_text(reason, f"Speed proposal abstention reason {index}")
    if diagnostics["method_note"] != METHOD_NOTE:
        raise ProjectValidationError("Speed proposal method note does not match.")
    if diagnostics["interpretation_note"] != INTERPRETATION_NOTE:
        raise ProjectValidationError(
            "Speed proposal interpretation note does not match."
        )

    estimate = _require_mapping(data["estimate"], "Speed proposal estimate")
    _require_exact_keys(
        estimate,
        required={
            "status",
            "confidence",
            "factor_definition",
            "proposed_factor",
            "diagnostic_center_factor",
            "sampling_confidence_interval_95",
            "robust_log_dispersion",
            "relative_dispersion",
            "rpm_hypothesis_status",
            "rpm_hypotheses",
        },
        optional=set(),
        label="Speed proposal estimate",
    )
    if estimate["status"] not in {"proposed", "abstained"}:
        raise ProjectValidationError("Speed proposal estimate status is unsupported.")
    if estimate["confidence"] not in {"high", "medium", "none"}:
        raise ProjectValidationError("Speed proposal confidence is unsupported.")
    if estimate["factor_definition"] != FACTOR_DEFINITION:
        raise ProjectValidationError("Speed proposal factor definition is unsupported.")
    if estimate["rpm_hypothesis_status"] != "hypotheses-only-not-inferred":
        raise ProjectValidationError(
            "Speed proposal RPM hypotheses must remain non-authoritative."
        )
    nullable_numbers = (
        "proposed_factor",
        "diagnostic_center_factor",
        "robust_log_dispersion",
        "relative_dispersion",
    )
    for key in nullable_numbers:
        if estimate[key] is not None:
            strict_finite_number(estimate[key], f"Speed proposal estimate {key}")
    interval = estimate["sampling_confidence_interval_95"]
    if interval is not None:
        if not isinstance(interval, list) or len(interval) != 2:
            raise ProjectValidationError(
                "Speed proposal confidence interval must contain two numbers."
            )
        lower = strict_finite_number(interval[0], "Speed proposal interval lower")
        upper = strict_finite_number(interval[1], "Speed proposal interval upper")
        if not 0 < lower <= upper:
            raise ProjectValidationError("Speed proposal confidence interval is invalid.")
    hypotheses = estimate["rpm_hypotheses"]
    if not isinstance(hypotheses, list) or len(hypotheses) > 8:
        raise ProjectValidationError(
            "Speed proposal RPM hypotheses must be a bounded array."
        )
    hypothesis_keys = {
        "rank",
        "nominal_pair",
        "capture_rpm",
        "intended_rpm",
        "coarse_factor",
        "fine_factor",
        "reconstructed_factor",
        "fine_delta_percent",
        "authority",
    }
    for index, item in enumerate(hypotheses, start=1):
        hypothesis = _require_mapping(item, f"RPM hypothesis {index}")
        _require_exact_keys(
            hypothesis,
            required=hypothesis_keys,
            optional=set(),
            label=f"RPM hypothesis {index}",
        )
        if type(hypothesis["rank"]) is not int or hypothesis["rank"] != index:
            raise ProjectValidationError("RPM hypothesis ranks must be consecutive.")
        _bounded_text(hypothesis["nominal_pair"], f"RPM hypothesis {index} pair")
        capture = hypothesis["capture_rpm"]
        intended = hypothesis["intended_rpm"]
        if (capture is None) != (intended is None):
            raise ProjectValidationError(
                "RPM hypothesis nominal values must both be present or absent."
            )
        if capture is not None and intended is not None:
            capture_value = strict_finite_number(
                capture, f"RPM hypothesis {index} capture RPM"
            )
            intended_value = strict_finite_number(
                intended, f"RPM hypothesis {index} intended RPM"
            )
            if not 10 <= capture_value <= 100 or not 10 <= intended_value <= 100:
                raise ProjectValidationError(
                    "RPM hypothesis nominal values must be in [10, 100]."
                )
        for key in (
            "coarse_factor",
            "fine_factor",
            "reconstructed_factor",
            "fine_delta_percent",
        ):
            strict_finite_number(
                hypothesis[key], f"RPM hypothesis {index} {key}"
            )
        if hypothesis["authority"] != "hypothesis-only-not-inferred":
            raise ProjectValidationError(
                "RPM hypothesis cannot claim selection or human approval."
            )
    if estimate["status"] == "proposed":
        if reasons or estimate["proposed_factor"] is None:
            raise ProjectValidationError(
                "A proposed speed factor cannot contain abstention reasons."
            )
        if estimate["confidence"] not in {"high", "medium"}:
            raise ProjectValidationError(
                "A proposed speed factor requires medium or high confidence."
            )
    elif estimate["proposed_factor"] is not None or estimate["confidence"] != "none":
        raise ProjectValidationError(
            "An abstained speed proposal cannot expose an actionable factor."
        )


def _validate_derived_proposal(data: dict[str, Any]) -> None:
    diagnostics = cast(dict[str, Any], data["diagnostics"])
    reference_identity = cast(dict[str, Any], data["reference_tracklist"])
    boundary_identity = data["boundary_review_evidence"]
    rows = cast(list[dict[str, Any]], data["tracks"])
    side_summaries = cast(list[dict[str, Any]], data["side_summaries"])
    project_count = cast(int, diagnostics["project_track_count"])
    reference_count = cast(int, diagnostics["reference_track_count"])
    if reference_count != reference_identity["track_count"]:
        raise ProjectValidationError(
            "Speed proposal reference counts do not match."
        )
    if project_count == reference_count:
        if len(rows) != project_count:
            raise ProjectValidationError(
                "Speed proposal row count does not match its input track counts."
            )
    elif rows:
        raise ProjectValidationError(
            "A count-mismatched speed proposal must not align partial track rows."
        )
    usable = sum(row["disposition"] == "candidate" for row in rows)
    excluded = sum(row["disposition"] == "excluded" for row in rows)
    if (
        diagnostics["usable_track_count"] != usable
        or diagnostics["excluded_track_count"] != excluded
        or usable + excluded != len(rows)
        or diagnostics["independent_side_count"] != len(side_summaries)
        or sum(side["usable_track_count"] for side in side_summaries) != usable
    ):
        raise ProjectValidationError(
            "Speed proposal diagnostic counts do not match its evidence rows."
        )
    reasons = cast(list[str], diagnostics["abstention_reasons"])
    if len(reasons) != len(set(reasons)):
        raise ProjectValidationError(
            "Speed proposal abstention reasons must be unique."
        )

    identity_mismatch = any(
        row["exclusion_reason"] in {
            "track_title_mismatch",
            "track_side_mismatch",
        }
        for row in rows
    )
    duration_status = diagnostics["reference_duration_independence_status"]
    if duration_status != reference_identity["duration_independence_status"]:
        raise ProjectValidationError(
            "Speed proposal duration-independence identities do not match."
        )
    boundary_status = diagnostics["boundary_review_status"]
    if boundary_identity is None:
        if boundary_status != "missing":
            raise ProjectValidationError(
                "Missing boundary review evidence must have missing status."
            )
    else:
        boundary_data = cast(dict[str, Any], boundary_identity)
        if boundary_data["status"] != boundary_status:
            raise ProjectValidationError(
                "Speed proposal boundary-review identities do not match."
            )
        project_identity = cast(dict[str, Any], data["project"])
        source_identity = cast(dict[str, Any], data["source"])
        exact_boundary_identity = (
            boundary_data["project_sha256"] == project_identity["sha256"]
            and boundary_data["project_revision"] == project_identity["revision"]
            and boundary_data["project_state_sha256"]
            == project_identity["state_sha256"]
            and boundary_data["source_sha256"] == source_identity["sha256"]
            and boundary_data["track_ranges_sha256"]
            == project_identity["track_ranges_sha256"]
        )
        if not exact_boundary_identity:
            expected_boundary_status = "stale"
        elif (
            not boundary_data["all_track_boundaries_reviewed"]
            or not boundary_data[
                "reviewed_boundaries_independent_of_reference_durations"
            ]
        ):
            expected_boundary_status = "inadequate"
        else:
            expected_boundary_status = "current"
        if boundary_status != expected_boundary_status:
            raise ProjectValidationError(
                "Speed proposal boundary-review status does not match its evidence."
            )

    safety_reasons: list[str] = []
    if duration_status == "unconfirmed":
        safety_reasons.append("reference_duration_independence_unconfirmed")
    elif duration_status == "not-independent":
        safety_reasons.append("reference_durations_not_independent")
    if boundary_status == "missing":
        safety_reasons.append("boundary_review_evidence_missing")
    elif boundary_status == "stale":
        safety_reasons.append("boundary_review_evidence_stale")
    elif boundary_status == "inadequate":
        safety_reasons.append("boundary_review_evidence_inadequate")

    identity_reasons: list[str] = []
    if project_count != reference_count:
        identity_reasons.append("track_count_mismatch")
    elif identity_mismatch:
        identity_reasons.append("reference_identity_mismatch")
    release_status = diagnostics["release_identity_status"]
    if release_status == "mismatch":
        identity_reasons.insert(0, "release_metadata_mismatch")
    elif release_status == "project-metadata-missing":
        identity_reasons.insert(0, "project_release_metadata_missing")

    config_values = cast(dict[str, Any], cast(dict[str, Any], data["config"])["values"])
    config = SpeedEstimatorConfig(**config_values)
    reconstructed_rows = [dict(row) for row in rows]
    for row in reconstructed_rows:
        if row["exclusion_reason"] == "robust_log_outlier":
            row["disposition"] = "candidate"
            row["exclusion_reason"] = None
    if reconstructed_rows and not identity_mismatch:
        _apply_outlier_rule(reconstructed_rows, config)
    if any(
        (
            reconstructed["disposition"],
            reconstructed["exclusion_reason"],
        )
        != (stored["disposition"], stored["exclusion_reason"])
        for reconstructed, stored in zip(reconstructed_rows, rows, strict=True)
    ):
        raise ProjectValidationError(
            "Speed proposal outlier exclusions do not match its bound config."
        )

    expected_estimate, expected_sides, statistical_reasons = _summarize_ratios(
        rows, config
    )
    expected_reasons = list(
        dict.fromkeys(safety_reasons + identity_reasons + statistical_reasons)
    )
    if expected_reasons:
        expected_estimate["status"] = "abstained"
        expected_estimate["confidence"] = "none"
        expected_estimate["proposed_factor"] = None
    if reasons != expected_reasons:
        raise ProjectValidationError(
            "Speed proposal abstention reasons do not match its evidence."
        )
    if side_summaries != expected_sides:
        raise ProjectValidationError(
            "Speed proposal side summaries do not match its evidence."
        )
    if data["estimate"] != expected_estimate:
        raise ProjectValidationError(
            "Speed proposal estimate does not match its evidence."
        )


def validate_speed_proposal(data: dict[str, Any]) -> None:
    """Validate schema, authority, nested shapes, and the proposal seal."""

    _require_exact_keys(
        data,
        required={
            "schema",
            "authority",
            "project",
            "source",
            "reference_tracklist",
            "boundary_review_evidence",
            "tool",
            "config",
            "tracks",
            "side_summaries",
            "diagnostics",
            "estimate",
            "proposal_sha256",
        },
        optional=set(),
        label="Speed proposal",
    )
    if data["schema"] != SPEED_PROPOSAL_SCHEMA:
        raise ProjectValidationError(
            f"Speed proposal schema must be '{SPEED_PROPOSAL_SCHEMA}'."
        )
    authority = _require_mapping(data["authority"], "Speed proposal authority")
    expected_authority = {
        "mode": "proposal-only",
        "may_apply_correction": False,
        "may_change_project": False,
        "human_approval": "not-inferred",
        "boundary_review": "owner-review-required-not-inferred",
    }
    if authority != expected_authority:
        raise ProjectValidationError(
            "Speed proposal authority must remain non-mutating and non-approving."
        )
    _validate_identity_objects(data)
    _validate_tool_and_config(data)
    _validate_proposal_rows(data)
    _validate_diagnostics_and_estimate(data)
    _validate_derived_proposal(data)
    expected_hash = _digest(data["proposal_sha256"], "Speed proposal SHA-256")
    without_hash = dict(data)
    del without_hash["proposal_sha256"]
    if expected_hash != canonical_json_sha256(without_hash):
        raise ProjectValidationError("Speed proposal SHA-256 seal does not match.")


def _write_new_bytes(payload: bytes, path: Path, *, label: str) -> Path:
    absolute = _absolute_without_resolving(path)
    absolute.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(absolute):
        raise ProjectValidationError(f"{label} output already exists: {absolute.name}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=absolute.parent,
        prefix=f".{absolute.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        rename_no_replace(temporary, absolute)
    finally:
        temporary.unlink(missing_ok=True)
    return absolute


def create_boundary_review_evidence(
    project_path: Path,
    output_path: Path,
    *,
    confirm_all_track_boundaries_reviewed: bool,
    confirm_review_independent_of_reference_durations: bool,
    reviewed_at: str | None = None,
) -> BoundaryReviewEvidence:
    """Record an explicit owner review attestation without approving correction."""

    if confirm_all_track_boundaries_reviewed is not True:
        raise ProjectValidationError(
            "Boundary-review evidence requires explicit confirmation that every "
            "track boundary received audio-and-visual review."
        )
    if confirm_review_independent_of_reference_durations is not True:
        raise ProjectValidationError(
            "Boundary-review evidence requires explicit confirmation that the "
            "review was independent of the reference durations."
        )
    absolute_project = _absolute_without_resolving(project_path)
    project, project_sha256 = load_project_with_sha256(absolute_project)
    _capture_current_source(project, absolute_project)
    repeated_project, repeated_sha256 = load_project_with_sha256(absolute_project)
    if (
        repeated_sha256 != project_sha256
        or repeated_project.state_sha256 != project.state_sha256
    ):
        raise ProjectValidationError(
            "The project changed while boundary-review evidence was being created."
        )
    evidence = BoundaryReviewEvidence(
        filename=_absolute_without_resolving(output_path).name,
        raw_sha256="0" * 64,
        canonical_sha256="0" * 64,
        project_sha256=project_sha256,
        project_revision=project.revision,
        project_state_sha256=project.state_sha256,
        source_sha256=project.source.sha256.lower(),
        track_ranges_sha256=project_track_ranges_sha256(project),
        reviewed_at=_review_timestamp(reviewed_at or utc_now_iso()),
        review_method="audio-and-visual-boundary-review",
        all_track_boundaries_reviewed=True,
        reviewed_boundaries_independent_of_reference_durations=True,
        correction_approval="not-granted",
    )
    payload = (
        json.dumps(
            evidence.canonical_dict(),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    absolute_output = _write_new_bytes(
        payload,
        output_path,
        label="Boundary review evidence",
    )
    return load_boundary_review_evidence(absolute_output)


def speed_proposal_bytes(data: dict[str, Any]) -> bytes:
    validate_speed_proposal(data)
    try:
        return (
            json.dumps(
                data,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProjectValidationError(
            f"Speed proposal is not finite JSON: {exc}"
        ) from exc


def write_speed_proposal(data: dict[str, Any], path: Path) -> str:
    """Atomically publish one new proposal without replacing an existing path."""

    payload = speed_proposal_bytes(data)
    _write_new_bytes(payload, path, label="Speed proposal")
    return cast(str, data["proposal_sha256"])


def load_speed_proposal(path: Path) -> dict[str, Any]:
    raw = _read_stable_plain_file(
        path, MAX_PROPOSAL_BYTES, "Speed proposal"
    )
    data = _decode_json_object(raw, "Speed proposal")
    validate_speed_proposal(data)
    return data


__all__ = [
    "BOUNDARY_REVIEW_SCHEMA",
    "DURATION_PROVENANCE_SCHEMA",
    "REFERENCE_TRACKLIST_SCHEMA",
    "SPEED_PROPOSAL_SCHEMA",
    "BoundaryReviewEvidence",
    "SpeedEstimatorConfig",
    "SpeedReferenceTracklist",
    "create_boundary_review_evidence",
    "estimate_speed",
    "estimate_speed_project",
    "load_boundary_review_evidence",
    "load_speed_proposal",
    "load_speed_reference_tracklist",
    "project_track_ranges_sha256",
    "speed_proposal_bytes",
    "validate_speed_proposal",
    "write_speed_proposal",
]
