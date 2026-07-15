from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from . import __version__
from .atomic_create import rename_no_replace
from .cache_storage import ensure_free_space
from .errors import ExportError, GrooveSerpentError, ProjectValidationError
from .exporter import (
    ExportReport,
    _build_command,
    _estimate_export_storage_bytes,
    _expected_track_sample_count,
    _resolve_portable_export_path,
    _verify_staged_output,
    export_project,
    sanitize_filename,
)
from .media import run_ffmpeg, sha256_file, tool_version
from .migration_fence import assert_no_pending_migration
from .models import Project, Track, resolve_source_path, utc_now_iso
from .portable_names import (
    portable_name_key,
    portable_path_entry_exists,
    portable_relative_path_key,
)
from .project_io import decode_project_json, load_project_with_sha256
from .publication import (
    FileReceipt,
    assert_file_receipt,
    canonical_json_sha256,
    capture_file_receipt,
    stage_verified_copy,
)
from .transaction_lock import (
    TargetWriteLease,
    canonical_target_path,
    exclusive_target_write_lease,
)
from .validation import strict_finite_number


ALBUM_SCHEMA = "groove-serpent.album/3"
ALBUM_SCHEMA_V2 = "groove-serpent.album/2"
LEGACY_ALBUM_SCHEMA = "groove-serpent.album/1"
ALBUM_EXPORT_SCHEMA = "groove-serpent.album-export/2"
ALBUM_CHAPTERS_SCHEMA = "groove-serpent.album-chapters/1"
ALBUM_MANIFEST_NAME = "groove-serpent-album-manifest.json"
ALBUM_CUE_NAME = "album.cue"
ALBUM_CHAPTERS_NAME = "album.chapters.json"
_STAGE_PREFIX = ".groove-serpent-album-"
_STAGE_SUFFIX = ".partial"
_MAX_ARTWORK_BYTES = 25 * 1024 * 1024
MAX_ALBUM_FILE_BYTES = 16 * 1024 * 1024
MAX_ALBUM_REVISION = (1 << 63) - 1
MAX_ALBUM_SIDES = 64
MAX_ALBUM_METADATA_ITEMS = 128
MAX_ALBUM_METADATA_KEY_LENGTH = 128
MAX_ALBUM_METADATA_VALUE_LENGTH = 4096
MAX_ALBUM_REFERENCE_LENGTH = 4096
MAX_ALBUM_TIMESTAMP_LENGTH = 64
_ARTWORK_SIGNATURES = {
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".png": b"\x89PNG\r\n\x1a\n",
}
_RESERVED_METADATA = {
    "cover_art_path",
    "cover_art_sha256",
    "track_number_offset",
    "album_track_total",
}


class _AutomaticExpectedAlbumState:
    pass


_AUTOMATIC_EXPECTED_ALBUM_STATE = _AutomaticExpectedAlbumState()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


@dataclass(frozen=True, slots=True)
class _AlbumFileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int

    @classmethod
    def capture(cls, value: os.stat_result) -> "_AlbumFileIdentity":
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            mode=value.st_mode,
            size=value.st_size,
            modified_ns=value.st_mtime_ns,
        )


def _is_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _plain_album_file_identity(path: Path) -> _AlbumFileIdentity:
    value = path.lstat()
    if (
        path.is_symlink()
        or _is_reparse(value)
        or not stat.S_ISREG(value.st_mode)
        or int(value.st_nlink) != 1
    ):
        raise ProjectValidationError(
            "Album-project path must be a single-link regular, non-reparse file: "
            f"{path.name}"
        )
    return _AlbumFileIdentity.capture(value)


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def canonical_album_path(path: Path) -> Path:
    """Canonicalize ancestors without ever following the final path component."""

    return canonical_target_path(path)


def _read_stable_album_bytes(
    path: Path, *, maximum: int | None = None
) -> tuple[bytes, _AlbumFileIdentity]:
    if maximum is None:
        maximum = MAX_ALBUM_FILE_BYTES
    before = _plain_album_file_identity(path)
    with path.open("rb") as handle:
        opened = _AlbumFileIdentity.capture(os.fstat(handle.fileno()))
        raw = handle.read(maximum + 1)
    after = _plain_album_file_identity(path)
    if before != opened or opened != after:
        raise ProjectValidationError(
            "Album-project file identity changed while it was being read."
        )
    if len(raw) > maximum:
        raise ProjectValidationError(
            f"Album project exceeds the {maximum}-byte file limit."
        )
    return raw, after


def _validate_album_timestamp(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_ALBUM_TIMESTAMP_LENGTH
    ):
        raise ProjectValidationError(
            f"{label} must be bounded non-empty ISO-8601 text."
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectValidationError(f"{label} must be valid ISO-8601 text.") from exc
    if parsed.tzinfo is None:
        raise ProjectValidationError(f"{label} must include a timezone.")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entry_exists(path: Path) -> bool:
    return portable_path_entry_exists(path)


def _strict_keys(data: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(data)
    missing = expected - actual
    extra = actual - expected
    if missing:
        raise ProjectValidationError(
            f"{label} is missing required field(s): {', '.join(sorted(missing))}."
        )
    if extra:
        raise ProjectValidationError(
            f"{label} contains unsupported field(s): {', '.join(sorted(extra))}."
        )


def _strict_number(value: Any, label: str) -> float:
    return strict_finite_number(value, label)


def _strict_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ProjectValidationError(
            f"{label} must be 64 lowercase hexadecimal characters."
        )
    return value


def _canonical_json_sha256(payload: dict[str, Any]) -> str:
    try:
        rendered = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ProjectValidationError(
            f"Identity state is not canonical JSON: {exc}"
        ) from exc
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _relative_reference(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_ALBUM_REFERENCE_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise ProjectValidationError(
            f"{label} must be 1-{MAX_ALBUM_REFERENCE_LENGTH} characters of "
            "trimmed printable text."
        )
    candidate = Path(value)
    if (
        candidate.is_absolute()
        or candidate.drive
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ProjectValidationError(
            f"{label} must be a relative path contained by the album-project folder."
        )
    return candidate.as_posix()


@dataclass(slots=True)
class AlbumArtwork:
    path: str
    sha256: str

    def validate(self) -> None:
        self.path = _relative_reference(self.path, "Album artwork path")
        if not isinstance(self.sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.sha256
        ):
            raise ProjectValidationError(
                "Album artwork SHA-256 must be 64 lowercase hexadecimal characters."
            )

    @classmethod
    def from_dict(cls, data: Any) -> "AlbumArtwork":
        if not isinstance(data, dict):
            raise ProjectValidationError("Album artwork must be a JSON object or null.")
        _strict_keys(data, {"path", "sha256"}, "Album artwork")
        artwork = cls(path=data["path"], sha256=data["sha256"])
        artwork.validate()
        return artwork


@dataclass(slots=True)
class SpeedState:
    """One canonical, validated fixed-speed correction state."""

    capture_rpm: float = 100.0 / 3.0
    intended_rpm: float = 100.0 / 3.0
    fine_factor: float = 1.0
    schema: str = "groove-serpent.speed-state/1"

    @property
    def effective_speed_factor(self) -> float:
        return self.capture_rpm / self.intended_rpm * self.fine_factor

    @property
    def sha256(self) -> str:
        return _canonical_json_sha256(
            {
                "schema": self.schema,
                "capture_rpm": self.capture_rpm,
                "intended_rpm": self.intended_rpm,
                "fine_factor": self.fine_factor,
            }
        )

    def validate(self) -> None:
        if self.schema != "groove-serpent.speed-state/1":
            raise ProjectValidationError(
                "Speed-state schema must be 'groove-serpent.speed-state/1'."
            )
        self.capture_rpm = _strict_number(self.capture_rpm, "Capture RPM")
        self.intended_rpm = _strict_number(self.intended_rpm, "Intended RPM")
        self.fine_factor = _strict_number(self.fine_factor, "Fine speed factor")
        if not 10.0 <= self.capture_rpm <= 100.0:
            raise ProjectValidationError("Capture RPM must be between 10 and 100.")
        if not 10.0 <= self.intended_rpm <= 100.0:
            raise ProjectValidationError("Intended RPM must be between 10 and 100.")
        if not 0.25 <= self.fine_factor <= 4.0:
            raise ProjectValidationError(
                "Fine speed factor must be between 0.25 and 4.0."
            )
        if not 0.25 <= self.effective_speed_factor <= 2.0:
            raise ProjectValidationError(
                "The combined capture/intended/fine speed factor must be between 0.25 and 2.0."
            )

    @classmethod
    def from_dict(cls, data: Any) -> "SpeedState":
        if not isinstance(data, dict):
            raise ProjectValidationError("Speed state must be a JSON object.")
        _strict_keys(
            data,
            {"schema", "capture_rpm", "intended_rpm", "fine_factor"},
            "Speed state",
        )
        state = cls(
            schema=data["schema"],
            capture_rpm=data["capture_rpm"],
            intended_rpm=data["intended_rpm"],
            fine_factor=data["fine_factor"],
        )
        state.validate()
        return state


def project_speed_state(project: Project) -> SpeedState:
    """Read the sole reviewed speed state from project metadata.

    A project with no speed fields has the explicit neutral default. A partial or
    malformed triplet is rejected instead of silently falling back to 1.0.
    """

    keys = ("speed_capture_rpm", "speed_intended_rpm", "speed_fine_factor")
    present = [key in project.metadata for key in keys]
    if not any(present):
        state = SpeedState()
        state.validate()
        return state
    if not all(present):
        missing = ", ".join(key for key, exists in zip(keys, present) if not exists)
        raise ProjectValidationError(
            f"Project speed metadata is incomplete; missing {missing}."
        )
    parsed: list[float] = []
    for key in keys:
        raw = project.metadata[key]
        if not isinstance(raw, str) or not raw or raw != raw.strip():
            raise ProjectValidationError(
                f"Project speed metadata {key!r} must be trimmed decimal text."
            )
        try:
            parsed.append(float(raw))
        except ValueError as exc:
            raise ProjectValidationError(
                f"Project speed metadata {key!r} must be decimal text."
            ) from exc
    state = SpeedState(
        capture_rpm=parsed[0],
        intended_rpm=parsed[1],
        fine_factor=parsed[2],
    )
    state.validate()
    return state


@dataclass(slots=True)
class AlbumSpeed:
    mode: str
    state: SpeedState
    state_sha256: str
    project_speed_state_sha256: str | None

    @classmethod
    def create(
        cls,
        mode: str,
        state: SpeedState,
        project_state: SpeedState | None,
    ) -> "AlbumSpeed":
        state.validate()
        speed = cls(
            mode=mode,
            state=copy.deepcopy(state),
            state_sha256=state.sha256,
            project_speed_state_sha256=(
                project_state.sha256 if project_state is not None else None
            ),
        )
        speed.validate()
        return speed

    def validate(self) -> None:
        if self.mode not in {"inherit", "override"}:
            raise ProjectValidationError(
                "Album speed mode must be 'inherit' or 'override'."
            )
        if not isinstance(self.state, SpeedState):
            raise ProjectValidationError(
                "Album speed state must use the SpeedState model."
            )
        self.state.validate()
        self.state_sha256 = _strict_digest(self.state_sha256, "Speed-state SHA-256")
        if self.state_sha256 != self.state.sha256:
            raise ProjectValidationError(
                "Album speed-state hash does not match its values."
            )
        if self.project_speed_state_sha256 is not None:
            self.project_speed_state_sha256 = _strict_digest(
                self.project_speed_state_sha256,
                "Project speed-state SHA-256",
            )
        if (
            self.mode == "inherit"
            and self.project_speed_state_sha256 is not None
            and self.state_sha256 != self.project_speed_state_sha256
        ):
            raise ProjectValidationError(
                "Inherited album speed must exactly match the pinned project speed state."
            )

    @classmethod
    def from_dict(cls, data: Any) -> "AlbumSpeed":
        if not isinstance(data, dict):
            raise ProjectValidationError("Album speed must be a JSON object.")
        _strict_keys(
            data,
            {"mode", "state", "state_sha256", "project_speed_state_sha256"},
            "Album speed",
        )
        speed = cls(
            mode=data["mode"],
            state=SpeedState.from_dict(data["state"]),
            state_sha256=data["state_sha256"],
            project_speed_state_sha256=data["project_speed_state_sha256"],
        )
        speed.validate()
        return speed


@dataclass(slots=True)
class AlbumSidePin:
    project_revision: int
    project_sha256: str
    editable_state_sha256: str
    source_sha256: str
    speed_state_sha256: str
    project_speed_state_sha256: str

    def validate(self) -> None:
        if type(self.project_revision) is not int or self.project_revision < 1:
            raise ProjectValidationError(
                "Pinned project revision must be a positive integer."
            )
        for field_name, label in (
            ("project_sha256", "Pinned project SHA-256"),
            ("editable_state_sha256", "Pinned editable-state SHA-256"),
            ("source_sha256", "Pinned source SHA-256"),
            ("speed_state_sha256", "Pinned speed-state SHA-256"),
            ("project_speed_state_sha256", "Pinned project speed-state SHA-256"),
        ):
            setattr(self, field_name, _strict_digest(getattr(self, field_name), label))

    @classmethod
    def from_dict(cls, data: Any) -> "AlbumSidePin":
        if not isinstance(data, dict):
            raise ProjectValidationError(
                "Album side pin must be a JSON object or null."
            )
        _strict_keys(
            data,
            {
                "project_revision",
                "project_sha256",
                "editable_state_sha256",
                "source_sha256",
                "speed_state_sha256",
                "project_speed_state_sha256",
            },
            "Album side pin",
        )
        pin = cls(**data)
        pin.validate()
        return pin


@dataclass(slots=True)
class AlbumSide:
    label: str
    order: int
    project: str
    speed: AlbumSpeed = field(
        default_factory=lambda: AlbumSpeed.create("inherit", SpeedState(), SpeedState())
    )
    pin: AlbumSidePin | None = None

    @property
    def capture_rpm(self) -> float:
        return self.speed.state.capture_rpm

    @property
    def intended_rpm(self) -> float:
        return self.speed.state.intended_rpm

    @property
    def fine_factor(self) -> float:
        return self.speed.state.fine_factor

    @property
    def effective_speed_factor(self) -> float:
        return self.speed.state.effective_speed_factor

    def validate(self) -> None:
        if (
            not isinstance(self.label, str)
            or not self.label
            or self.label != self.label.strip()
            or len(self.label) > 32
            or any(ord(character) < 32 for character in self.label)
        ):
            raise ProjectValidationError(
                "Album side labels must be 1-32 characters of trimmed printable text."
            )
        if type(self.order) is not int or not 1 <= self.order <= 999:
            raise ProjectValidationError(
                "Album side order must be a JSON integer between 1 and 999."
            )
        self.project = _relative_reference(self.project, "Album side project reference")
        if not isinstance(self.speed, AlbumSpeed):
            raise ProjectValidationError(
                "Album side speed must use the AlbumSpeed model."
            )
        self.speed.validate()
        if self.pin is not None:
            if not isinstance(self.pin, AlbumSidePin):
                raise ProjectValidationError(
                    "Album side pin must use the AlbumSidePin model."
                )
            self.pin.validate()
            if self.pin.speed_state_sha256 != self.speed.state_sha256:
                raise ProjectValidationError(
                    "Pinned speed-state hash does not match album speed."
                )
            if (
                self.speed.project_speed_state_sha256 is None
                or self.pin.project_speed_state_sha256
                != self.speed.project_speed_state_sha256
            ):
                raise ProjectValidationError(
                    "Pinned project speed-state hash does not match album speed provenance."
                )

    @classmethod
    def from_dict(cls, data: Any) -> "AlbumSide":
        if not isinstance(data, dict):
            raise ProjectValidationError("Each album side must be a JSON object.")
        _strict_keys(data, {"label", "order", "project", "speed", "pin"}, "Album side")
        side = cls(
            label=data["label"],
            order=data["order"],
            project=data["project"],
            speed=AlbumSpeed.from_dict(data["speed"]),
            pin=(
                AlbumSidePin.from_dict(data["pin"]) if data["pin"] is not None else None
            ),
        )
        side.validate()
        return side

    @classmethod
    def from_legacy_dict(cls, data: Any) -> "AlbumSide":
        if not isinstance(data, dict):
            raise ProjectValidationError(
                "Each legacy album side must be a JSON object."
            )
        _strict_keys(
            data,
            {
                "label",
                "order",
                "project",
                "capture_rpm",
                "intended_rpm",
                "fine_factor",
            },
            "Legacy album side",
        )
        state = SpeedState(
            capture_rpm=data["capture_rpm"],
            intended_rpm=data["intended_rpm"],
            fine_factor=data["fine_factor"],
        )
        state.validate()
        side = cls(
            label=data["label"],
            order=data["order"],
            project=data["project"],
            speed=AlbumSpeed.create("override", state, None),
            pin=None,
        )
        side.validate()
        return side


@dataclass(slots=True)
class AlbumProject:
    metadata: dict[str, str]
    sides: list[AlbumSide]
    artwork: AlbumArtwork | None = None
    revision: int = 1
    schema: str = ALBUM_SCHEMA
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def validate(self) -> None:
        if self.schema != ALBUM_SCHEMA:
            raise ProjectValidationError(
                f"Unsupported album schema {self.schema!r}; expected {ALBUM_SCHEMA!r}."
            )
        if (
            type(self.revision) is not int
            or not 1 <= self.revision <= MAX_ALBUM_REVISION
        ):
            raise ProjectValidationError(
                "Album revision must be a bounded positive integer."
            )
        _validate_album_timestamp(self.created_at, "Album created_at")
        _validate_album_timestamp(self.updated_at, "Album updated_at")
        if not isinstance(self.metadata, dict):
            raise ProjectValidationError("Album metadata must be a JSON object.")
        if len(self.metadata) > MAX_ALBUM_METADATA_ITEMS:
            raise ProjectValidationError(
                f"Album metadata cannot exceed {MAX_ALBUM_METADATA_ITEMS} entries."
            )
        for key, value in self.metadata.items():
            if (
                not isinstance(key, str)
                or not key
                or key != key.strip()
                or len(key) > MAX_ALBUM_METADATA_KEY_LENGTH
                or not isinstance(value, str)
                or len(value) > MAX_ALBUM_METADATA_VALUE_LENGTH
            ):
                raise ProjectValidationError(
                    "Album metadata keys and values must be bounded text, and keys "
                    "must be non-empty and trimmed."
                )
            if key in _RESERVED_METADATA:
                raise ProjectValidationError(
                    f"Album metadata field {key!r} is reserved by the exporter."
                )
        if (
            not isinstance(self.sides, list)
            or not 1 <= len(self.sides) <= MAX_ALBUM_SIDES
        ):
            raise ProjectValidationError(
                f"An album project must contain 1-{MAX_ALBUM_SIDES} sides."
            )
        labels: set[str] = set()
        projects: set[str] = set()
        for expected_order, side in enumerate(self.sides, start=1):
            if not isinstance(side, AlbumSide):
                raise ProjectValidationError(
                    "Album sides must use the AlbumSide model."
                )
            side.validate()
            if side.order != expected_order:
                raise ProjectValidationError(
                    "Album side order must be consecutive, start at 1, and match list order."
                )
            folded_label = portable_name_key(side.label)
            folded_project = portable_relative_path_key(side.project)
            if folded_label in labels:
                raise ProjectValidationError(
                    f"Duplicate album side label: {side.label!r}."
                )
            if folded_project in projects:
                raise ProjectValidationError(
                    f"Duplicate album side project reference: {side.project!r}."
                )
            labels.add(folded_label)
            projects.add(folded_project)
        if self.artwork is not None:
            if not isinstance(self.artwork, AlbumArtwork):
                raise ProjectValidationError(
                    "Album artwork must use the AlbumArtwork model."
                )
            self.artwork.validate()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "AlbumProject":
        if not isinstance(data, dict):
            raise ProjectValidationError(
                "The album-project root must be a JSON object."
            )
        schema = data.get("schema")
        if schema in {LEGACY_ALBUM_SCHEMA, ALBUM_SCHEMA_V2}:
            raise ProjectValidationError(
                f"Album schema {schema!r} is legacy. Run "
                "'groove-serpent album migrate ALBUM' before opening it."
            )
        if schema != ALBUM_SCHEMA:
            raise ProjectValidationError(
                f"Unsupported album schema {schema!r}; expected {ALBUM_SCHEMA!r}."
            )
        _strict_keys(
            data,
            {
                "schema",
                "revision",
                "created_at",
                "updated_at",
                "metadata",
                "artwork",
                "sides",
            },
            "Album project",
        )
        metadata = data["metadata"]
        if not isinstance(metadata, dict):
            raise ProjectValidationError("Album metadata must be a JSON object.")
        raw_sides = data["sides"]
        if not isinstance(raw_sides, list):
            raise ProjectValidationError("Album sides must be a JSON array.")
        album = cls(
            schema=ALBUM_SCHEMA,
            revision=data["revision"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            metadata=dict(metadata),
            artwork=(
                AlbumArtwork.from_dict(data["artwork"])
                if data["artwork"] is not None
                else None
            ),
            sides=[AlbumSide.from_dict(item) for item in raw_sides],
        )
        album.validate()
        return album


@dataclass(slots=True)
class AlbumExportReport:
    output_directory: str
    files: list[dict[str, Any]]
    manifest_path: str
    cue_path: str
    chapters_path: str = ""


def save_album_project(
    album: AlbumProject,
    path: Path,
    *,
    overwrite: bool = False,
    expected_existing_sha256: (
        str | None | _AutomaticExpectedAlbumState
    ) = _AUTOMATIC_EXPECTED_ALBUM_STATE,
) -> None:
    """Save one album under an OS-backed compare-and-swap write lease.

    ``None`` means the caller observed no exact or portable-equivalent entry.
    A digest binds replacement to the exact bytes the caller loaded. Omitting
    the argument retains the legacy function-entry snapshot for internal and
    test compatibility.
    """

    path = _absolute_without_resolving(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path = canonical_album_path(path)
    automatic_expectation = (
        expected_existing_sha256 is _AUTOMATIC_EXPECTED_ALBUM_STATE
    )
    if not automatic_expectation and expected_existing_sha256 is not None:
        if (
            not isinstance(expected_existing_sha256, str)
            or len(expected_existing_sha256) != 64
            or expected_existing_sha256.lower() != expected_existing_sha256
            or any(
                character not in "0123456789abcdef"
                for character in expected_existing_sha256
            )
        ):
            raise ProjectValidationError(
                "Expected album SHA-256 must be a lowercase 64-character digest."
            )
    exact_before_lease = os.path.lexists(path)
    portable_before_lease = _entry_exists(path)
    identity_before_lease: _AlbumFileIdentity | None = None
    sha256_before_lease: str | None = None
    if exact_before_lease:
        identity_before_lease = _plain_album_file_identity(path)
        raw_before_lease, repeated_identity = _read_stable_album_bytes(path)
        if repeated_identity != identity_before_lease:
            raise ProjectValidationError(
                "Album-project identity changed before the write lease was acquired."
            )
        sha256_before_lease = hashlib.sha256(raw_before_lease).hexdigest()
    if not automatic_expectation:
        expected_exists = expected_existing_sha256 is not None
        if expected_exists != exact_before_lease:
            raise ProjectValidationError(
                "Album-project path no longer matches the caller's expected "
                "existence state."
            )
        if not expected_exists and portable_before_lease:
            raise ProjectValidationError(
                "An NFC/case-equivalent album-project destination already exists."
            )
        if (
            expected_exists
            and sha256_before_lease != expected_existing_sha256
        ):
            raise ProjectValidationError(
                "Album project changed after the caller loaded it; reload before saving."
            )
    with exclusive_target_write_lease(path) as write_lease:
        _save_album_project_locked(
            album,
            path,
            overwrite=overwrite,
            write_lease=write_lease,
            exact_before_lease=exact_before_lease,
            portable_before_lease=portable_before_lease,
            identity_before_lease=identity_before_lease,
            sha256_before_lease=sha256_before_lease,
        )


def _save_album_project_locked(
    album: AlbumProject,
    path: Path,
    *,
    overwrite: bool,
    write_lease: TargetWriteLease,
    exact_before_lease: bool,
    portable_before_lease: bool,
    identity_before_lease: _AlbumFileIdentity | None,
    sha256_before_lease: str | None,
) -> None:
    write_lease.assert_current()
    assert_no_pending_migration(path, "album")
    album.validate()
    path = _absolute_without_resolving(path)
    if path.parent.exists():
        path = canonical_album_path(path)
    exact_exists = os.path.lexists(path)
    portable_exists = _entry_exists(path)
    if (
        exact_exists != exact_before_lease
        or portable_exists != portable_before_lease
    ):
        raise ProjectValidationError(
            "Album-project path existence changed while waiting for the write lease."
        )
    original_identity: _AlbumFileIdentity | None = None
    original_sha256: str | None = None
    if exact_exists:
        original_identity = _plain_album_file_identity(path)
    elif portable_exists:
        raise ProjectValidationError(
            "An NFC/case-equivalent album-project destination already exists."
        )
    if exact_exists and not overwrite:
        raise ProjectValidationError(
            f"Album project already exists: {path}. Use --overwrite to replace it."
        )
    if exact_exists:
        original_raw, read_identity = _read_stable_album_bytes(path)
        if read_identity != original_identity:
            raise ProjectValidationError(
                "Album-project identity changed before overwrite preparation."
            )
        if (
            read_identity != identity_before_lease
            or hashlib.sha256(original_raw).hexdigest() != sha256_before_lease
        ):
            raise ProjectValidationError(
                "Album project changed while waiting for the write lease."
            )
        existing = AlbumProject.from_dict(decode_project_json(original_raw))
        if existing.revision != album.revision:
            raise ProjectValidationError(
                "Album-project revision changed; reload before saving."
            )
        if existing.revision >= MAX_ALBUM_REVISION:
            raise ProjectValidationError(
                "Album-project revision is exhausted and cannot be incremented."
            )
        original_sha256 = hashlib.sha256(original_raw).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    next_updated_at = utc_now_iso()
    next_revision = album.revision + 1 if exact_exists else album.revision
    payload = album.to_dict()
    payload["updated_at"] = next_updated_at
    payload["revision"] = next_revision
    text = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > MAX_ALBUM_FILE_BYTES:
        raise ProjectValidationError(
            f"Album project exceeds the {MAX_ALBUM_FILE_BYTES}-byte file limit."
        )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if original_identity is None:
            if os.path.lexists(path) or _entry_exists(path):
                raise ProjectValidationError(
                    "Album-project destination appeared before atomic save."
                )
        else:
            repeated_raw, repeated_identity = _read_stable_album_bytes(path)
            if (
                repeated_identity != original_identity
                or hashlib.sha256(repeated_raw).hexdigest() != original_sha256
            ):
                raise ProjectValidationError(
                    "Album-project identity changed before atomic save."
                )
        write_lease.assert_current()
        if original_identity is None:
            try:
                rename_no_replace(temporary, path)
            except FileExistsError as exc:
                raise ProjectValidationError(
                    "Album-project destination appeared before atomic creation."
                ) from exc
        else:
            os.replace(temporary, path)
        album.updated_at = next_updated_at
        album.revision = next_revision
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def load_album_project_with_sha256(path: Path) -> tuple[AlbumProject, str]:
    path = canonical_album_path(path)
    try:
        raw, _ = _read_stable_album_bytes(path)
        data = decode_project_json(raw)
        return AlbumProject.from_dict(data), hashlib.sha256(raw).hexdigest()
    except ProjectValidationError:
        raise
    except (
        AttributeError,
        KeyError,
        OSError,
        OverflowError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise ProjectValidationError(f"Album project is invalid: {exc}") from exc


def load_album_project(path: Path) -> AlbumProject:
    return load_album_project_with_sha256(path)[0]


def _resolve_contained_final(
    root: Path, candidate: Path, label: str
) -> Path:
    root = root.resolve()
    try:
        parent = candidate.parent.resolve()
        parent.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectValidationError(
            f"{label} must remain inside the album-project folder."
        ) from exc
    resolved = parent / candidate.name
    if os.path.lexists(resolved):
        value = resolved.lstat()
        if resolved.is_symlink() or _is_reparse(value):
            raise ProjectValidationError(
                f"{label} must not be a symlink, junction, or reparse point."
            )
    return resolved


def resolve_album_reference(album_path: Path, reference: str, label: str) -> Path:
    normalized = _relative_reference(reference, label)
    root = canonical_album_path(album_path).parent
    return _resolve_contained_final(root, root / normalized, label)


def _current_side_identity(
    side: AlbumSide,
    album_path: Path,
) -> tuple[Path, Project, str, Path, str, SpeedState]:
    project_path = resolve_album_reference(
        album_path, side.project, "Album side project reference"
    )
    project, project_sha256 = load_project_with_sha256(project_path)
    source_path = resolve_source_path(project, project_path).resolve()
    if not source_path.is_file():
        raise ProjectValidationError(
            f"Side {side.label} source audio does not exist: {source_path}"
        )
    actual_source_sha256 = sha256_file(source_path).lower()
    speed_state = project_speed_state(project)
    return (
        project_path,
        project,
        project_sha256,
        source_path,
        actual_source_sha256,
        speed_state,
    )


def pin_album_side(side: AlbumSide, album_path: Path) -> AlbumSidePin:
    """Approve and pin the side project's exact current publication identity."""

    side.validate()
    (
        _project_path,
        project,
        project_sha256,
        _source_path,
        actual_source_sha256,
        current_project_speed,
    ) = _current_side_identity(side, album_path)
    if project.source.sha256 and actual_source_sha256 != project.source.sha256.lower():
        raise ProjectValidationError(
            f"Side {side.label} source no longer matches its project SHA-256; it cannot be pinned."
        )
    if side.speed.mode == "inherit":
        side.speed = AlbumSpeed.create(
            "inherit", current_project_speed, current_project_speed
        )
    else:
        side.speed = AlbumSpeed.create(
            "override", side.speed.state, current_project_speed
        )
    side.pin = AlbumSidePin(
        project_revision=project.revision,
        project_sha256=project_sha256,
        editable_state_sha256=project.state_sha256,
        source_sha256=actual_source_sha256,
        speed_state_sha256=side.speed.state_sha256,
        project_speed_state_sha256=current_project_speed.sha256,
    )
    side.validate()
    return side.pin


def repin_album_sides(
    album: AlbumProject,
    album_path: Path,
    labels: Iterable[str] | None = None,
) -> list[str]:
    """Explicitly approve current state for selected sides (or every side)."""

    album.validate()
    selected = None if labels is None else [portable_name_key(label) for label in labels]
    if selected is not None:
        if not selected:
            raise ProjectValidationError("Choose at least one side to repin.")
        if len(selected) != len(set(selected)):
            raise ProjectValidationError(
                "A side label may be selected for repin only once."
            )
        known = {portable_name_key(side.label) for side in album.sides}
        unknown = sorted(set(selected) - known)
        if unknown:
            raise ProjectValidationError(
                f"Unknown album side label(s): {', '.join(unknown)}."
            )
    repinned: list[str] = []
    for side in album.sides:
        if selected is None or portable_name_key(side.label) in selected:
            pin_album_side(side, album_path)
            repinned.append(side.label)
    album.validate()
    return repinned


def _side_identity_status(
    side: AlbumSide,
    album_path: Path,
) -> tuple[dict[str, Any], tuple[Path, Project, str, Path, str, SpeedState]]:
    current = _current_side_identity(side, album_path)
    project_path, project, project_sha256, source_path, source_sha256, speed_state = (
        current
    )
    drift: list[str] = []
    if side.pin is None:
        drift.append("side is unpinned")
    else:
        comparisons = (
            (
                side.pin.project_revision,
                project.revision,
                "project revision changed",
            ),
            (side.pin.project_sha256, project_sha256, "project file changed"),
            (
                side.pin.editable_state_sha256,
                project.state_sha256,
                "editable project state changed",
            ),
            (side.pin.source_sha256, source_sha256, "source audio changed"),
            (
                side.pin.project_speed_state_sha256,
                speed_state.sha256,
                "reviewed project speed state changed",
            ),
            (
                side.pin.speed_state_sha256,
                side.speed.state_sha256,
                "album speed selection changed",
            ),
        )
        drift.extend(
            message for expected, actual, message in comparisons if expected != actual
        )
    if project.source.sha256 and source_sha256 != project.source.sha256.lower():
        drift.append("source no longer matches the side project")
    status = {
        "pinned": side.pin is not None,
        "ready_for_export": not drift,
        "drift": drift,
        "speed_mode": side.speed.mode,
        "speed_override": side.speed.mode == "override",
        "speed_override_differs_from_project": (
            side.speed.state_sha256 != speed_state.sha256
        ),
        "selected_speed_state_sha256": side.speed.state_sha256,
        "project_speed_state_sha256": speed_state.sha256,
        "pin": asdict(side.pin) if side.pin is not None else None,
        "current": {
            "project_revision": project.revision,
            "project_sha256": project_sha256,
            "editable_state_sha256": project.state_sha256,
            "source_sha256": source_sha256,
            "project_speed_state_sha256": speed_state.sha256,
        },
        "resolved_project": str(project_path),
        "resolved_source": str(source_path),
    }
    return status, current


def artwork_for_album_path(album_path: Path, supplied_path: Path) -> AlbumArtwork:
    album_path = canonical_album_path(album_path)
    root = album_path.parent
    supplied_path = supplied_path.expanduser()
    if supplied_path.drive and not supplied_path.is_absolute():
        raise ProjectValidationError(
            "Album artwork must be an absolute path or relative to the album-project folder."
        )
    candidate = (
        _absolute_without_resolving(supplied_path)
        if supplied_path.is_absolute()
        else root / supplied_path
    )
    supplied_path = _resolve_contained_final(root, candidate, "Album artwork")
    try:
        relative = supplied_path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ProjectValidationError(
            "Album artwork must be contained by the album-project folder."
        ) from exc
    artwork = AlbumArtwork(path=relative, sha256=_validated_artwork(supplied_path))
    artwork.validate()
    return artwork


def _validated_artwork(path: Path) -> str:
    if not path.is_file():
        raise ProjectValidationError(f"Album artwork does not exist: {path}")
    if path.stat().st_size > _MAX_ARTWORK_BYTES:
        raise ProjectValidationError("Album artwork exceeds the 25 MB limit.")
    signature = _ARTWORK_SIGNATURES.get(path.suffix.casefold())
    if signature is None:
        raise ProjectValidationError("Album artwork must be JPEG or PNG.")
    with path.open("rb") as handle:
        if not handle.read(8).startswith(signature):
            raise ProjectValidationError(
                "Album artwork content does not match its filename extension."
            )
    return _sha256(path)


def parse_album_side_spec(value: str, order: int, album_path: Path) -> AlbumSide:
    """Parse a pinned side, inheriting project speed unless explicitly overridden."""

    if not isinstance(value, str):
        raise ProjectValidationError("Album side specifications must be text.")
    parts = value.split("|")
    if len(parts) not in {2, 5}:
        raise ProjectValidationError(
            "Each --side must be LABEL|PROJECT or "
            "LABEL|PROJECT|CAPTURE_RPM|INTENDED_RPM|FINE_FACTOR."
        )
    label, supplied_project = parts[0], parts[1]
    root = canonical_album_path(album_path).parent
    project_path = Path(supplied_project).expanduser()
    if project_path.drive and not project_path.is_absolute():
        raise ProjectValidationError(
            "Album side projects must be absolute paths or relative to the album-project folder."
        )
    candidate = (
        _absolute_without_resolving(project_path)
        if project_path.is_absolute()
        else root / project_path
    )
    project_path = _resolve_contained_final(
        root, candidate, "Album side project"
    )
    if not project_path.is_file():
        raise ProjectValidationError(
            f"Album side project does not exist: {project_path}"
        )
    try:
        project_reference = project_path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ProjectValidationError(
            "Every side project must be contained by the album-project folder."
        ) from exc
    state = SpeedState()
    mode = "inherit"
    if len(parts) == 5:
        try:
            values = [float(part) for part in parts[2:]]
        except ValueError as exc:
            raise ProjectValidationError(
                "Side RPM and fine-factor values must be finite decimal numbers."
            ) from exc
        state = SpeedState(
            capture_rpm=values[0],
            intended_rpm=values[1],
            fine_factor=values[2],
        )
        state.validate()
        mode = "override"
    side = AlbumSide(
        label=label,
        order=order,
        project=project_reference,
        speed=AlbumSpeed.create(mode, state, state if mode == "inherit" else None),
    )
    side.validate()
    pin_album_side(side, album_path)
    return side


def inspect_album_project(album: AlbumProject, album_path: Path) -> dict[str, Any]:
    album.validate()
    album_path = canonical_album_path(album_path)
    result: dict[str, Any] = {
        "schema": album.schema,
        "album_project": str(album_path),
        "album_project_sha256": _sha256(album_path),
        "metadata": dict(album.metadata),
        "artwork": asdict(album.artwork) if album.artwork is not None else None,
        "total_tracks": 0,
        "ready_for_export": True,
        "sides": [],
    }
    for side in album.sides:
        status, current = _side_identity_status(side, album_path)
        (
            project_path,
            project,
            project_sha256,
            _source_path,
            source_sha256,
            speed_state,
        ) = current
        result["total_tracks"] += len(project.tracks)
        result["ready_for_export"] = bool(
            result["ready_for_export"] and status["ready_for_export"]
        )
        result["sides"].append(
            {
                "order": side.order,
                "label": side.label,
                "project": side.project,
                "project_sha256": project_sha256,
                "project_revision": project.revision,
                "editable_state_sha256": project.state_sha256,
                "source": project.source.filename,
                "source_sha256": source_sha256,
                "tracks": len(project.tracks),
                "capture_rpm": side.capture_rpm,
                "intended_rpm": side.intended_rpm,
                "fine_factor": side.fine_factor,
                "effective_speed_factor": side.effective_speed_factor,
                "speed_mode": side.speed.mode,
                "selected_speed_state_sha256": side.speed.state_sha256,
                "project_speed_state_sha256": speed_state.sha256,
                **status,
            }
        )
    if album.artwork is not None:
        artwork_path = resolve_album_reference(
            album_path, album.artwork.path, "Album artwork path"
        )
        actual = _validated_artwork(artwork_path)
        if actual != album.artwork.sha256:
            raise ProjectValidationError(
                "Album artwork no longer matches the SHA-256 recorded in the album project."
            )
    return result


def suggest_album_output_directory(album: AlbumProject, album_path: Path) -> Path:
    album_path, _ = _resolve_portable_export_path(
        album_path,
        context="album-project path used for the export suggestion",
    )
    parent, parent_exists = _resolve_portable_export_path(
        album_path.parent / "album-exports",
        context="album export suggestion parent",
    )
    if parent_exists and not parent.is_dir():
        raise ExportError("The album export suggestion parent is not a directory.")
    artist = album.metadata.get("album_artist") or album.metadata.get("artist", "")
    title = album.metadata.get("album") or album.metadata.get("title", "")
    label = " - ".join(part for part in (artist, title) if part)
    base_name = sanitize_filename(label, album_path.stem)
    candidate, candidate_exists = _resolve_portable_export_path(
        parent / base_name,
        context="album export batch suggestion",
    )
    suffix = 2
    while candidate_exists:
        name_suffix = f" ({suffix})"
        candidate, candidate_exists = _resolve_portable_export_path(
            parent
            / (
                f"{sanitize_filename(label, album_path.stem, suffix=name_suffix)}"
                f"{name_suffix}"
            ),
            context="album export batch suggestion",
        )
        suffix += 1
    return candidate


def _cue_quote(value: str) -> str:
    printable = " ".join(str(value).replace("\x00", " ").splitlines())
    printable = " ".join(printable.split())
    # CUE has no portable escape sequence for a literal quote. Two apostrophes
    # preserve the visible punctuation without allowing a new directive.
    return f'"{printable.replace(chr(34), chr(39) * 2)}"'


def _cue_time(sample: int, sample_rate: int) -> str:
    frames = (sample * 75 + sample_rate // 2) // sample_rate
    minutes, remainder = divmod(frames, 75 * 60)
    seconds, cue_frames = divmod(remainder, 75)
    return f"{minutes:02d}:{seconds:02d}:{cue_frames:02d}"


def _render_cue(
    album: AlbumProject,
    side_receipts: list[dict[str, Any]],
) -> str:
    total_tracks = sum(len(side["tracks"]) for side in side_receipts)
    if total_tracks > 99:
        raise ExportError(
            "CUE publication is limited to 99 album tracks because common CUE consumers "
            "require two-digit track numbers. Split the album into separately approved volumes."
        )
    album_artist = album.metadata.get("album_artist") or album.metadata.get(
        "artist", ""
    )
    album_title = album.metadata.get("album") or album.metadata.get("title", "")
    lines = [
        'REM GENERATED_BY "Groove Serpent"',
        'REM INDEX_PRECISION "75 fps approximate; album.chapters.json is exact"',
        f"PERFORMER {_cue_quote(album_artist)}",
        f"TITLE {_cue_quote(album_title)}",
    ]
    for side in side_receipts:
        lines.append(f"REM SIDE {_cue_quote(side['label'])}")
        lines.append(f"FILE {_cue_quote(side['continuous_file']['path'])} WAVE")
        running_sample = 0
        for track in side["tracks"]:
            lines.extend(
                [
                    f"  TRACK {track['album_track_number']:02d} AUDIO",
                    f"    TITLE {_cue_quote(track['title'])}",
                    f"    PERFORMER {_cue_quote(track['artist'])}",
                    f"    INDEX 01 {_cue_time(running_sample, side['sample_rate'])}",
                ]
            )
            running_sample += track["expected_output_sample_count"]
    return "\n".join(lines) + "\n"


def _chapters_payload(
    album: AlbumProject,
    album_sha256: str,
    side_receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": ALBUM_CHAPTERS_SCHEMA,
        "album_project_sha256": album_sha256,
        "metadata": dict(album.metadata),
        "precision": "exact integer sample positions",
        "cue_companion": {
            "path": ALBUM_CUE_NAME,
            "timebase_frames_per_second": 75,
            "precision": "approximate rounded navigation indexes",
        },
        "total_tracks": sum(len(side["tracks"]) for side in side_receipts),
        "sides": [
            {
                "order": side["order"],
                "label": side["label"],
                "file": side["continuous_file"]["path"],
                "source_sample_rate": side["sample_rate"],
                "output_sample_rate": side["sample_rate"],
                "output_sample_count": side["expected_output_sample_count"],
                "tracks": [
                    {
                        "album_track_number": track["album_track_number"],
                        "local_track_number": track["local_track_number"],
                        "title": track["title"],
                        "artist": track["artist"],
                        "source_start_sample": track["source_start_sample"],
                        "source_end_sample": track["source_end_sample"],
                        "source_sample_rate": side["sample_rate"],
                        "output_start_sample": track["side_output_start_sample"],
                        "output_end_sample": track["side_output_end_sample"],
                        "output_sample_rate": side["sample_rate"],
                    }
                    for track in side["tracks"]
                ],
            }
            for side in side_receipts
        ],
    }


def _clone_for_album_export(
    project: Project,
    side: AlbumSide,
    album: AlbumProject,
    *,
    offset: int,
    total_tracks: int,
    source_path: Path,
    artwork_path: Path | None,
    artwork_receipt: FileReceipt | None,
    virtual_root: Path,
) -> Project:
    cloned = copy.deepcopy(project)
    cloned.source.path = str(source_path)
    cloned.metadata.update(album.metadata)
    for reserved in _RESERVED_METADATA:
        cloned.metadata.pop(reserved, None)
    cloned.metadata["track_number_offset"] = str(offset)
    cloned.metadata["album_track_total"] = str(total_tracks)
    if artwork_path is not None and album.artwork is not None:
        if artwork_receipt is None:
            raise ExportError("Album artwork has no verified snapshot receipt.")
        virtual_root.mkdir(parents=True, exist_ok=True)
        virtual_artwork = virtual_root / f"cover{artwork_path.suffix.casefold()}"
        virtual_artwork_receipt = stage_verified_copy(
            artwork_path,
            virtual_artwork,
            artwork_receipt,
            label=f"Side {side.label} artwork",
        )
        if virtual_artwork_receipt.sha256 != album.artwork.sha256:
            raise ExportError(
                f"The verified Side {side.label} artwork copy does not match the "
                "approved album artwork."
            )
        cloned.metadata["cover_art_path"] = virtual_artwork.name
        cloned.metadata["cover_art_sha256"] = album.artwork.sha256

    shared_fields = {
        "artist": album.metadata.get("artist", ""),
        "album": album.metadata.get("album") or album.metadata.get("title", ""),
        "album_artist": album.metadata.get("album_artist", ""),
        "year": album.metadata.get("year", ""),
        "genre": album.metadata.get("genre", ""),
    }
    for track in cloned.tracks:
        for field_name, value in shared_fields.items():
            if value:
                setattr(track, field_name, value)
        track.side = side.label
    cloned.validate()
    return cloned


def _write_continuous_side(
    *,
    project: Project,
    source_path: Path,
    destination: Path,
    side: AlbumSide,
    total_tracks: int,
    first_album_track: int,
    flac_compression: int,
    artwork_path: Path | None,
) -> dict[str, Any]:
    first = project.tracks[0]
    last = project.tracks[-1]
    speed_factor = side.effective_speed_factor
    correction_factor = (
        None if math.isclose(speed_factor, 1.0, abs_tol=1e-12) else speed_factor
    )
    side_track = Track(
        number=first_album_track,
        title=(project.tracks[0].album or "Album") + f" - Side {side.label}",
        start_sample=first.start_sample,
        end_sample=last.end_sample,
        start_seconds=first.start_sample / project.source.sample_rate,
        end_seconds=last.end_sample / project.source.sample_rate,
        artist=project.tracks[0].artist,
        album=project.tracks[0].album,
        album_artist=project.tracks[0].album_artist,
        year=project.tracks[0].year,
        genre=project.tracks[0].genre,
        side=side.label,
    )
    command = _build_command(
        source_path=source_path,
        output_path=destination,
        track=side_track,
        total_tracks=total_tracks,
        output_format="flac",
        source_sample_rate=project.source.sample_rate,
        source_bits=project.source.bits_per_raw_sample,
        overwrite=True,
        flac_compression=flac_compression,
        aac_bitrate="256k",
        artwork_path=artwork_path,
        project_metadata=project.metadata,
        source_speed_factor=correction_factor,
    )
    run_ffmpeg(command)
    if not destination.is_file():
        raise ExportError(
            f"FFmpeg did not create the continuous Side {side.label} FLAC."
        )
    expected = _expected_track_sample_count(
        side_track, project.source.sample_rate, correction_factor
    )
    verification = _verify_staged_output(
        staged_path=destination,
        source_snapshot=source_path,
        track=side_track,
        output_format="flac",
        expected_sample_count=expected,
        source_sample_rate=project.source.sample_rate,
        source_channels=project.source.channels,
        source_bits=project.source.bits_per_raw_sample,
        source_speed_factor=correction_factor,
        total_tracks=total_tracks,
    )
    return {
        "path": destination.as_posix(),
        "size_bytes": destination.stat().st_size,
        "sha256": sha256_file(destination),
        "expected_sample_count": expected,
        "presentation_sample_count": expected,
        "verification": {
            "codec_name": verification.codec_name,
            "sample_rate": verification.sample_rate,
            "channels": verification.channels,
            "bits_per_raw_sample": verification.bits_per_raw_sample,
            "complete_decode_verified": True,
            **(
                {"decoded_pcm_sha256": verification.decoded_pcm_sha256}
                if verification.decoded_pcm_sha256 is not None
                else {}
            ),
            **(
                {
                    "source_range_pcm_sha256": (verification.source_range_pcm_sha256),
                    "archival_pcm_equal": True,
                }
                if verification.source_range_pcm_sha256 is not None
                else {}
            ),
        },
    }


def _cleanup_stage(stage: Path, parent: Path) -> None:
    if stage.parent != parent or not (
        stage.name.startswith(_STAGE_PREFIX) and stage.name.endswith(_STAGE_SUFFIX)
    ):
        raise ExportError(
            f"Refusing to remove an unexpected album staging path: {stage}"
        )
    if not _entry_exists(stage):
        return
    if stage.is_symlink() or not stage.is_dir():
        stage.unlink()
    else:
        shutil.rmtree(stage)


def _inventory_file(
    root: Path,
    path: Path,
    role: str,
    **receipt: Any,
) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    return {
        "role": role,
        "path": relative,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
        **receipt,
    }


def _assert_inventory_consistent(root: Path, inventory: list[dict[str, Any]]) -> None:
    """Rehash every staged artifact against the top-level manifest inventory."""

    for item in inventory:
        relative = item.get("path")
        expected_size = item.get("size_bytes")
        expected_sha256 = item.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or type(expected_size) is not int
            or not isinstance(expected_sha256, str)
        ):
            raise ExportError("The album publication inventory is malformed.")
        candidate = (root / Path(relative)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ExportError(
                f"Album inventory path {relative!r} escapes the staging directory."
            ) from exc
        receipt = capture_file_receipt(
            candidate, label=f"Staged album artifact {relative}"
        )
        if receipt.size_bytes != expected_size or receipt.sha256 != expected_sha256:
            raise ExportError(
                f"Staged album artifact {relative!r} no longer matches its inventory."
            )


def _estimate_album_storage_bytes(
    projects: Iterable[tuple[Project, float | None]],
    formats: Iterable[str],
    *,
    artwork_size_bytes: int = 0,
) -> int:
    """Estimate conservative peak space for the album publication graph.

    Each side budgets one ordinary multi-format track batch and a second FLAC
    batch for the continuous-side derivative. This intentionally overcounts
    per-track container slack for the continuous file, covering the two source
    copies that coexist during nested atomic publication. Shared artwork gets
    two additional budgets for the work snapshot and published cover.
    """

    selected_formats = tuple(formats)
    required = 4 * 1024 * 1024 + 2 * artwork_size_bytes
    for project, speed_factor in projects:
        required += _estimate_export_storage_bytes(
            project,
            selected_formats,
            speed_factor,
            artwork_size_bytes=artwork_size_bytes,
        )
        required += _estimate_export_storage_bytes(
            project,
            ("flac",),
            speed_factor,
            artwork_size_bytes=artwork_size_bytes,
        )
    return required


def export_album(
    album: AlbumProject,
    album_path: Path,
    output_dir: Path,
    *,
    formats: Iterable[str] = ("flac", "m4a"),
    flac_compression: int = 8,
    aac_bitrate: str = "256k",
    progress: Callable[[str], None] | None = None,
) -> AlbumExportReport:
    album.validate()
    album_path, _ = _resolve_portable_export_path(
        album_path,
        context="album-project path",
    )
    output_dir, output_exists = _resolve_portable_export_path(
        output_dir,
        context="album output directory",
    )
    if type(flac_compression) is not int or not 0 <= flac_compression <= 12:
        raise ExportError("FLAC compression must be an integer between 0 and 12.")
    bitrate_match = (
        re.fullmatch(r"([1-9][0-9]{1,3})k", aac_bitrate)
        if isinstance(aac_bitrate, str)
        else None
    )
    if bitrate_match is None or not 32 <= int(bitrate_match.group(1)) <= 512:
        raise ExportError("AAC bitrate must be a whole value from 32k through 512k.")
    requested: list[str] = []
    for raw in formats:
        if not isinstance(raw, str):
            raise ExportError("Album export formats must be text.")
        normalized = raw.strip().lower()
        if normalized == "aac":
            normalized = "m4a"
        if normalized not in {"flac", "m4a"}:
            raise ExportError("Album formats must be FLAC and/or M4A.")
        if normalized not in requested:
            requested.append(normalized)
    if not requested:
        raise ExportError("At least one album track format is required.")
    if output_exists:
        raise ExportError(
            "The album output directory already exists. Choose a new batch directory."
        )
    output_dir, output_appeared = _resolve_portable_export_path(
        output_dir,
        context="album output directory",
        create_parents=True,
    )
    if not output_dir.parent.is_dir():
        raise ExportError("The album output parent is not a directory.")
    if output_appeared:
        raise ExportError(
            "The album output directory was created by another process. "
            "Choose a new batch directory."
        )

    album_file_receipt = capture_file_receipt(album_path, label="Album project")
    album_sha256 = album_file_receipt.sha256
    artwork_path: Path | None = None
    artwork_file_receipt: FileReceipt | None = None
    if album.artwork is not None:
        artwork_path = resolve_album_reference(
            album_path, album.artwork.path, "Album artwork path"
        )
        artwork_sha256 = _validated_artwork(artwork_path)
        if artwork_sha256 != album.artwork.sha256:
            raise ExportError(
                "Album artwork no longer matches the SHA-256 recorded in the album project."
            )
        artwork_file_receipt = capture_file_receipt(artwork_path, label="Album artwork")
        if artwork_file_receipt.sha256 != album.artwork.sha256:
            raise ExportError("Album artwork changed while export was starting.")

    loaded_sides: list[
        tuple[
            AlbumSide,
            Path,
            Project,
            str,
            Path,
            str,
            FileReceipt,
            FileReceipt,
        ]
    ] = []
    total_tracks = 0
    for side in album.sides:
        status, current = _side_identity_status(side, album_path)
        (
            project_path,
            project,
            project_sha256,
            source_path,
            actual_source_sha256,
            _project_speed,
        ) = current
        if not status["ready_for_export"]:
            raise ExportError(
                f"Side {side.label} is not pinned to its current approved state: "
                f"{'; '.join(status['drift'])}. Run 'groove-serpent album repin' "
                "for that side after reviewing the changes."
            )
        if not 0.25 <= side.effective_speed_factor <= 2.0:
            raise ExportError(
                f"Side {side.label} speed factor {side.effective_speed_factor:.9f} is "
                "representable in the album project, but the current exporter is tested only "
                "from 0.25 through 2.0. Publication fails closed outside that verified range."
            )
        project_file_receipt = capture_file_receipt(
            project_path, label=f"Side {side.label} project"
        )
        source_file_receipt = capture_file_receipt(
            source_path, label=f"Side {side.label} source"
        )
        if project_file_receipt.sha256 != project_sha256:
            raise ExportError(
                f"Side {side.label} project changed while album export was starting."
            )
        if source_file_receipt.sha256 != actual_source_sha256:
            raise ExportError(
                f"Side {side.label} source changed while album export was starting."
            )
        loaded_sides.append(
            (
                side,
                project_path,
                project,
                project_sha256,
                source_path,
                actual_source_sha256,
                project_file_receipt,
                source_file_receipt,
            )
        )
        total_tracks += len(project.tracks)
    if total_tracks > 99:
        raise ExportError(
            "Album export refuses more than 99 tracks because the required CUE companion "
            "cannot represent them portably. Split the album into separately approved volumes."
        )

    storage_projects = [
        (
            item[2],
            (
                None
                if math.isclose(item[0].effective_speed_factor, 1.0, abs_tol=1e-12)
                else item[0].effective_speed_factor
            ),
        )
        for item in loaded_sides
    ]
    storage_required = _estimate_album_storage_bytes(
        storage_projects,
        requested,
        artwork_size_bytes=(
            artwork_file_receipt.size_bytes
            if artwork_file_receipt is not None
            else 0
        ),
    )
    try:
        ensure_free_space(
            output_dir.parent,
            storage_required,
            label="Album export",
        )
    except GrooveSerpentError as exc:
        raise ExportError(str(exc)) from exc

    stage = output_dir.parent / f"{_STAGE_PREFIX}{uuid.uuid4().hex}{_STAGE_SUFFIX}"
    created = False
    inventory: list[dict[str, Any]] = []
    side_receipts: list[dict[str, Any]] = []
    published_copy_receipts: list[tuple[Path, FileReceipt, str]] = []
    try:
        stage.mkdir()
        created = True
        tracks_root = stage / "tracks"
        sides_root = stage / "sides"
        manifests_root = stage / "side-manifests"
        work_root = stage / ".work"
        for directory in (tracks_root, sides_root, manifests_root, work_root):
            directory.mkdir()

        published_artwork: dict[str, Any] | None = None
        published_artwork_receipt: FileReceipt | None = None
        artwork_snapshot: Path | None = None
        artwork_snapshot_receipt: FileReceipt | None = None
        if (
            artwork_path is not None
            and album.artwork is not None
            and artwork_file_receipt is not None
        ):
            artwork_snapshot = work_root / (
                "album-artwork" + artwork_path.suffix.casefold()
            )
            artwork_snapshot_receipt = stage_verified_copy(
                artwork_path,
                artwork_snapshot,
                artwork_file_receipt,
                label="Album artwork",
            )
            artwork_root = stage / "artwork"
            artwork_root.mkdir()
            published_path = artwork_root / f"cover{artwork_path.suffix.casefold()}"
            published_artwork_receipt = stage_verified_copy(
                artwork_snapshot,
                published_path,
                artwork_snapshot_receipt,
                label="Published album artwork",
            )
            if published_artwork_receipt.sha256 != album.artwork.sha256:
                raise ExportError(
                    "The published album artwork does not match the approved artwork."
                )
            published_artwork = {
                "path": published_path.relative_to(stage).as_posix(),
                "sha256": published_artwork_receipt.sha256,
                "file_identity": artwork_file_receipt.identity_dict(),
            }
            artwork_inventory = _inventory_file(stage, published_path, "artwork")
            if (
                artwork_inventory["sha256"] != published_artwork_receipt.sha256
                or artwork_inventory["size_bytes"]
                != published_artwork_receipt.size_bytes
            ):
                raise ExportError(
                    "The final album artwork copy failed its verified receipt."
                )
            inventory.append(artwork_inventory)
            published_copy_receipts.append(
                (
                    published_path,
                    published_artwork_receipt,
                    "Published album artwork",
                )
            )

        offset = 0
        for (
            side,
            project_path,
            project,
            project_sha256,
            source_path,
            actual_source_sha256,
            project_file_receipt,
            source_file_receipt,
        ) in loaded_sides:
            if progress:
                progress(
                    f"Exporting album Side {side.label} ({len(project.tracks)} tracks)"
                )
            virtual_root = work_root / f"input-{side.order:02d}"
            virtual_root.mkdir()
            source_snapshot = virtual_root / (
                "source" + (source_path.suffix.casefold() or ".audio")
            )
            source_snapshot_receipt = stage_verified_copy(
                source_path,
                source_snapshot,
                source_file_receipt,
                label=f"Side {side.label} source",
            )
            cloned = _clone_for_album_export(
                project,
                side,
                album,
                offset=offset,
                total_tracks=total_tracks,
                source_path=source_snapshot,
                artwork_path=artwork_snapshot,
                artwork_receipt=artwork_snapshot_receipt,
                virtual_root=virtual_root,
            )
            virtual_project_path = virtual_root / f"side-{side.order:02d}.groove.json"
            side_batch = work_root / f"export-{side.order:02d}"
            correction_factor = (
                None
                if math.isclose(side.effective_speed_factor, 1.0, abs_tol=1e-12)
                else side.effective_speed_factor
            )
            report: ExportReport = export_project(
                cloned,
                virtual_project_path,
                side_batch,
                formats=requested,
                overwrite=False,
                flac_compression=flac_compression,
                aac_bitrate=aac_bitrate,
                source_speed_factor=correction_factor,
                progress=progress,
            )

            track_receipts: list[dict[str, Any]] = []
            exported_by_track: dict[int, dict[str, Any]] = {}
            for item in report.files:
                supplied = Path(item.path)
                if supplied.is_absolute() or supplied.name != item.path:
                    raise ExportError("A side exporter returned an unsafe track path.")
                source_export = side_batch / item.path
                destination = tracks_root / supplied.name
                if _entry_exists(destination):
                    raise ExportError(
                        f"Album track filename collision: {destination.name!r}."
                    )
                shutil.move(source_export, destination)
                file_receipt = _inventory_file(
                    stage,
                    destination,
                    "track",
                    track_number=item.track_number,
                    format=item.format,
                    expected_sample_count=item.expected_sample_count,
                    **(
                        {"presentation_sample_count": item.presentation_sample_count}
                        if item.presentation_sample_count is not None
                        else {}
                    ),
                )
                if file_receipt["sha256"] != item.sha256:
                    raise ExportError(
                        f"Moved album track {destination.name!r} failed its SHA-256 receipt."
                    )
                inventory.append(file_receipt)
                exported_by_track.setdefault(item.track_number, {})[item.format] = (
                    file_receipt
                )

            side_manifest_source = Path(report.manifest_path)
            side_prefix = f"{side.order:02d} - Side "
            side_manifest_suffix = ".json"
            side_manifest_label = sanitize_filename(
                side.label,
                str(side.order),
                prefix=side_prefix,
                suffix=side_manifest_suffix,
            )
            side_manifest_name = (
                f"{side_prefix}{side_manifest_label}{side_manifest_suffix}"
            )
            side_manifest_path = manifests_root / side_manifest_name
            side_manifest_source_receipt = capture_file_receipt(
                side_manifest_source,
                label=f"Side {side.label} publication manifest",
            )
            side_manifest_receipt = stage_verified_copy(
                side_manifest_source,
                side_manifest_path,
                side_manifest_source_receipt,
                label=f"Published Side {side.label} manifest",
            )
            side_manifest_inventory = _inventory_file(
                stage, side_manifest_path, "side-manifest"
            )
            if (
                side_manifest_inventory["sha256"] != side_manifest_receipt.sha256
                or side_manifest_inventory["size_bytes"]
                != side_manifest_receipt.size_bytes
            ):
                raise ExportError(
                    f"The copied Side {side.label} manifest failed its verified receipt."
                )
            inventory.append(side_manifest_inventory)
            published_copy_receipts.append(
                (
                    side_manifest_path,
                    side_manifest_receipt,
                    f"Published Side {side.label} manifest",
                )
            )

            side_audio_suffix = ".flac"
            side_audio_label = sanitize_filename(
                side.label,
                str(side.order),
                prefix=side_prefix,
                suffix=side_audio_suffix,
            )
            side_filename = f"{side_prefix}{side_audio_label}{side_audio_suffix}"
            continuous_path = sides_root / side_filename
            virtual_artwork = None
            if artwork_snapshot is not None:
                virtual_artwork = (
                    virtual_root / f"cover{artwork_snapshot.suffix.casefold()}"
                )
            continuous = _write_continuous_side(
                project=cloned,
                source_path=source_snapshot,
                destination=continuous_path,
                side=side,
                total_tracks=total_tracks,
                first_album_track=offset + 1,
                flac_compression=flac_compression,
                artwork_path=virtual_artwork,
            )
            continuous["path"] = continuous_path.relative_to(stage).as_posix()
            assert_file_receipt(
                source_snapshot,
                source_snapshot_receipt,
                label=f"Staged Side {side.label} source snapshot",
            )
            inventory.append(
                _inventory_file(
                    stage,
                    continuous_path,
                    "continuous-side",
                    expected_sample_count=continuous["expected_sample_count"],
                    presentation_sample_count=continuous["presentation_sample_count"],
                )
            )

            running_output = 0
            for local_index, track in enumerate(cloned.tracks, start=1):
                album_number = offset + local_index
                expected = _expected_track_sample_count(
                    track, cloned.source.sample_rate, correction_factor
                )
                receipt = {
                    "local_track_number": local_index,
                    "album_track_number": album_number,
                    "title": track.title,
                    "artist": track.artist,
                    "source_start_sample": track.start_sample,
                    "source_end_sample": track.end_sample,
                    "source_sample_count": track.end_sample - track.start_sample,
                    "side_output_start_sample": running_output,
                    "side_output_end_sample": running_output + expected,
                    "expected_output_sample_count": expected,
                    "files": exported_by_track.get(album_number, {}),
                }
                track_receipts.append(receipt)
                running_output += expected

            side_manifest_payload = json.loads(
                side_manifest_path.read_text(encoding="utf-8")
            )
            speed_receipt = {
                "mode": side.speed.mode,
                "capture_rpm": side.capture_rpm,
                "intended_rpm": side.intended_rpm,
                "fine_factor": side.fine_factor,
                "requested_effective_speed_factor": side.effective_speed_factor,
                "speed_state_sha256": side.speed.state_sha256,
                "project_speed_state_sha256": side.speed.project_speed_state_sha256,
                "override_differs_from_project": (
                    side.speed.state_sha256 != side.speed.project_speed_state_sha256
                ),
                "applied": correction_factor is not None,
            }
            if isinstance(side_manifest_payload.get("speed_correction"), dict):
                speed_receipt.update(side_manifest_payload["speed_correction"])
            side_receipts.append(
                {
                    "order": side.order,
                    "label": side.label,
                    "project": side.project,
                    "project_sha256": project_sha256,
                    "project_revision": project.revision,
                    "editable_state_sha256": project.state_sha256,
                    "project_file_identity": project_file_receipt.identity_dict(),
                    "approved_pin": asdict(side.pin) if side.pin is not None else None,
                    "source": project.source.filename,
                    "source_sha256": actual_source_sha256,
                    "source_file_identity": source_file_receipt.identity_dict(),
                    "sample_rate": project.source.sample_rate,
                    "speed": speed_receipt,
                    "track_number_start": offset + 1,
                    "track_number_end": offset + len(project.tracks),
                    "source_start_sample": project.tracks[0].start_sample,
                    "source_end_sample": project.tracks[-1].end_sample,
                    "source_sample_count": (
                        project.tracks[-1].end_sample - project.tracks[0].start_sample
                    ),
                    "expected_output_sample_count": running_output,
                    "continuous_file": continuous,
                    "side_manifest": side_manifest_path.relative_to(stage).as_posix(),
                    "tracks": track_receipts,
                }
            )
            if continuous["expected_sample_count"] != running_output:
                raise ExportError(
                    f"Side {side.label} track receipts do not sum to its continuous FLAC."
                )
            offset += len(project.tracks)

        cue_path = stage / ALBUM_CUE_NAME
        with cue_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(_render_cue(album, side_receipts))
            handle.flush()
            os.fsync(handle.fileno())
        cue_inventory = _inventory_file(
            stage,
            cue_path,
            "cue-sheet",
            timebase_frames_per_second=75,
            boundary_precision="approximate rounded navigation indexes",
        )
        inventory.append(cue_inventory)

        chapters_path = stage / ALBUM_CHAPTERS_NAME
        with chapters_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                _chapters_payload(album, album_sha256, side_receipts),
                handle,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        chapters_inventory = _inventory_file(
            stage,
            chapters_path,
            "exact-chapters",
            position_unit="integer samples",
        )
        inventory.append(chapters_inventory)

        if artwork_snapshot is not None and artwork_snapshot_receipt is not None:
            assert_file_receipt(
                artwork_snapshot,
                artwork_snapshot_receipt,
                label="Staged album artwork snapshot",
            )
        shutil.rmtree(work_root)

        toolchain = {
            "ffmpeg": tool_version("ffmpeg"),
            "ffprobe": tool_version("ffprobe"),
        }
        output_profile = {
            "name": "album-publication",
            "restoration": "none",
            "side_speed_modes": [
                {
                    "label": side["label"],
                    "mode": side["speed"]["mode"],
                    "applied": side["speed"]["applied"],
                }
                for side in side_receipts
            ],
            "continuous_sides": "lossless FLAC",
            "track_formats": list(requested),
        }
        encoder_settings = {
            "flac_compression": flac_compression,
            "aac_bitrate": aac_bitrate,
            "aac_encoder": "FFmpeg native aac",
        }
        processing_plan = {
            "schema": "groove-serpent.album-processing-plan/1",
            "groove_serpent_version": __version__,
            "album_project_sha256": album_sha256,
            "formats": list(requested),
            "encoder_settings": encoder_settings,
            "output_profile": output_profile,
            "operation_order": [
                "verify pinned album and side identities",
                "create immutable source and artwork snapshots",
                "render continuously numbered exact tracks",
                "render one verified continuous FLAC per side",
                "write approximate CUE and exact sample chapters",
                "revalidate every live input",
                "atomic album-directory publication",
            ],
            "sides": [
                {
                    "label": side["label"],
                    "approved_pin": side["approved_pin"],
                    "speed": side["speed"],
                    "track_number_start": side["track_number_start"],
                    "track_number_end": side["track_number_end"],
                }
                for side in side_receipts
            ],
            "toolchain": toolchain,
        }
        manifest = {
            "schema": ALBUM_EXPORT_SCHEMA,
            "groove_serpent_version": __version__,
            "created_at": utc_now_iso(),
            "album_project": album_path.name,
            "album_project_sha256": album_sha256,
            "album_project_identity": album_file_receipt.manifest_dict(),
            "metadata": dict(album.metadata),
            "artwork": published_artwork,
            "formats": requested,
            "total_tracks": total_tracks,
            "output_profile": output_profile,
            "toolchain": toolchain,
            "encoder_settings": encoder_settings,
            "processing_plan": processing_plan,
            "processing_plan_sha256": canonical_json_sha256(processing_plan),
            "verification": {
                "pinned_side_states_matched": True,
                "immutable_operation_snapshots": True,
                "all_tracks_inherit_verified_side_manifests": True,
                "continuous_side_flacs_completely_decoded": True,
                "prepublication_input_revalidation": "matched",
                "publication": "atomic-directory-rename",
            },
            "cue": {
                "path": cue_inventory["path"],
                "sha256": cue_inventory["sha256"],
                "timebase_frames_per_second": 75,
                "boundary_precision": "approximate rounded navigation indexes",
            },
            "chapters": {
                "path": chapters_inventory["path"],
                "sha256": chapters_inventory["sha256"],
                "position_unit": "integer samples",
                "precision": "exact",
            },
            "sides": side_receipts,
            "files": sorted(
                inventory, key=lambda item: portable_relative_path_key(item["path"])
            ),
        }
        manifest_path = stage / ALBUM_MANIFEST_NAME
        with manifest_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        for copied_path, copied_receipt, copied_label in published_copy_receipts:
            assert_file_receipt(
                copied_path,
                copied_receipt,
                label=copied_label,
            )
        _assert_inventory_consistent(stage, inventory)

        # Revalidate again after every staged byte, immediately before the
        # publication rename. Immutable snapshots keep outputs internally
        # consistent; this final lease check prevents stale provenance.
        for (
            _,
            project_path,
            project,
            _,
            source_path,
            _,
            project_file_receipt,
            source_file_receipt,
        ) in loaded_sides:
            assert_file_receipt(
                project_path,
                project_file_receipt,
                label=f"Side project {project_path.name}",
            )
            assert_file_receipt(
                source_path,
                source_file_receipt,
                label=f"Source audio {project.source.filename}",
            )
        assert_file_receipt(album_path, album_file_receipt, label="Album project")
        if artwork_path is not None and artwork_file_receipt is not None:
            assert_file_receipt(
                artwork_path, artwork_file_receipt, label="Album artwork"
            )

        if _entry_exists(output_dir):
            raise ExportError(
                "The album output directory was created while staging; nothing was replaced."
            )
        rename_no_replace(stage, output_dir)
        created = False
    except BaseException as exc:
        cleanup_error: Exception | None = None
        if created:
            try:
                _cleanup_stage(stage, output_dir.parent)
            except (
                Exception
            ) as cleanup_exc:  # pragma: no cover - rare filesystem failure
                cleanup_error = cleanup_exc
        if not isinstance(exc, Exception):
            raise
        if (
            isinstance(exc, (ExportError, ProjectValidationError))
            and cleanup_error is None
        ):
            raise
        message = (
            str(exc)
            if isinstance(exc, (ExportError, ProjectValidationError))
            else f"Album export failed before a complete batch could be published: {exc}"
        )
        if cleanup_error is not None:
            message += f" Staging cleanup also failed at {stage}: {cleanup_error}"
        raise ExportError(message) from exc

    return AlbumExportReport(
        output_directory=str(output_dir),
        files=inventory,
        manifest_path=str(output_dir / ALBUM_MANIFEST_NAME),
        cue_path=str(output_dir / ALBUM_CUE_NAME),
        chapters_path=str(output_dir / ALBUM_CHAPTERS_NAME),
    )
