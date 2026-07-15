"""Private, inspectable evidence memory that can never grant approval.

This module deliberately stops at persistence.  It does not feed proposals back
into analysis, restoration, recognition, speed correction, or publication.  A
future integration may use records as evidence, but must still ask the owner to
review and approve each action in the project where it will be applied.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, Mapping, cast

from .atomic_create import rename_no_replace
from .errors import GrooveSerpentError


RECORD_SCHEMA = "groove-serpent.review-evidence-record/1"
SETTINGS_SCHEMA = "groove-serpent.review-evidence-settings/1"
EXPORT_SCHEMA = "groove-serpent.review-evidence-export/1"
EVIDENCE_AUTHORITY = "evidence-only-never-approval"

EvidenceCategory = Literal[
    "boundary",
    "endpoint",
    "structural-event",
    "restoration",
    "recognition",
    "speed",
]
OwnerOutcome = Literal["accepted", "rejected", "adjusted", "protected"]

EVIDENCE_CATEGORIES: frozenset[str] = frozenset(
    {"boundary", "endpoint", "structural-event", "restoration", "recognition", "speed"}
)
OWNER_OUTCOMES: frozenset[str] = frozenset(
    {"accepted", "rejected", "adjusted", "protected"}
)

MAX_RECORD_BYTES = 256 * 1024
MAX_EXPORT_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 4_096
MAX_JSON_DEPTH = 12
MAX_JSON_ITEMS = 2_048
MAX_TEXT_LENGTH = 4_096
_READ_CHUNK_BYTES = 1024 * 1024
_REPARSE_POINT = 0x400
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RECORD_NAME = re.compile(r"^([0-9a-f]{64})\.json$")
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "api_key",
        "audio",
        "base64",
        "bearer",
        "blob",
        "credential",
        "data_uri",
        "file",
        "password",
        "path",
        "pcm",
        "secret",
        "samples",
        "spectrogram",
        "token",
        "waveform",
    }
)


class ReviewEvidenceError(GrooveSerpentError):
    """A review-evidence store or record failed closed."""


@dataclass(frozen=True, slots=True)
class StoredReviewEvidence:
    """One verified immutable record in the local store."""

    record_sha256: str
    path: Path
    category: EvidenceCategory
    outcome: OwnerOutcome
    recorded_at: str
    _canonical_payload: str = field(compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return a detached copy of the validated record."""

        return cast(dict[str, Any], json.loads(self._canonical_payload))

    def summary_dict(self) -> dict[str, Any]:
        """Return a deterministic, path-free inventory entry."""

        payload = self.to_dict()
        return {
            "record_sha256": self.record_sha256,
            "category": self.category,
            "outcome": self.outcome,
            "recorded_at": self.recorded_at,
            "project_sha256": payload["project"]["sha256"],
            "source_sha256": payload["source"]["sha256"],
            "region": payload["region"],
        }


@dataclass(frozen=True, slots=True)
class ReviewEvidenceStatus:
    """Current explicit settings and verified record count."""

    root: Path
    enabled: bool
    configured: bool
    record_count: int
    authority: str = EVIDENCE_AUTHORITY

    def to_dict(self, *, include_root: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": SETTINGS_SCHEMA,
            "enabled": self.enabled,
            "configured": self.configured,
            "record_count": self.record_count,
            "authority": self.authority,
            "may_authorize_action": False,
            "may_apply_action": False,
        }
        if include_root:
            result["root"] = str(self.root)
        return result


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def capture(cls, value: os.stat_result) -> "_FileIdentity":
        return cls(
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )


def _same_handle_file_object(left: _FileIdentity, right: _FileIdentity) -> bool:
    """Compare handle/path observations despite synthesized Windows metadata."""

    return (
        left.device == right.device
        and left.inode == right.inode
        and stat.S_IFMT(left.mode) == stat.S_IFMT(right.mode)
        and left.size == right.size
        and left.modified_ns == right.modified_ns
    )


def _same_directory_object(left: _FileIdentity, right: _FileIdentity) -> bool:
    return (
        left.device == right.device
        and left.inode == right.inode
        and stat.S_IFMT(left.mode) == stat.S_IFMT(right.mode)
    )


def review_evidence_may_authorize_action() -> Literal[False]:
    """Return the permanent authority boundary for every corpus result."""

    return False


def review_evidence_may_apply_action() -> Literal[False]:
    """Return the permanent mutation boundary for every corpus result."""

    return False


def default_review_evidence_root() -> Path:
    """Return the private per-user default without creating it."""

    configured = os.environ.get("GROOVE_SERPENT_REVIEW_EVIDENCE_DIR")
    if configured:
        return _absolute(Path(configured))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return _absolute(Path(local) / "Groove Serpent" / "review-evidence")
    return _absolute(Path.home() / ".groove-serpent" / "review-evidence")


def resolve_review_evidence_root(configured: str | os.PathLike[str] | None) -> Path:
    """Resolve an explicit root or the private per-user default."""

    return _absolute(Path(configured)) if configured is not None else default_review_evidence_root()


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", _REPARSE_POINT))
    return bool(attributes & flag)


def _plain_identity(path: Path, *, directory: bool) -> _FileIdentity:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReviewEvidenceError(f"Review-evidence path cannot be inspected: {path.name}") from exc
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise ReviewEvidenceError(
            f"Review-evidence paths cannot be symlinks, junctions, or reparse points: {path.name}"
        )
    matches = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not matches:
        expected = "directory" if directory else "regular file"
        raise ReviewEvidenceError(f"Review-evidence path must be a {expected}: {path.name}")
    return _FileIdentity.capture(metadata)


def _ensure_root(root: Path, *, create: bool) -> bool:
    root = _absolute(root)
    if os.path.lexists(root):
        _plain_identity(root, directory=True)
        return True
    if not create:
        return False
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ReviewEvidenceError("The review-evidence root could not be created.") from exc
    _plain_identity(root, directory=True)
    return True


def _ensure_records_directory(root: Path, *, create: bool) -> bool:
    records = root / "records"
    if os.path.lexists(records):
        _plain_identity(records, directory=True)
        return True
    if not create:
        return False
    try:
        records.mkdir(exist_ok=False)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ReviewEvidenceError("The records directory could not be created.") from exc
    _plain_identity(records, directory=True)
    return True


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Invalid JSON number: {value}")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def _decode_json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReviewEvidenceError(
            f"{label} is not strict, finite, duplicate-free UTF-8 JSON."
        ) from exc
    if type(payload) is not dict:
        raise ReviewEvidenceError(f"{label} root must be an object.")
    return cast(dict[str, Any], payload)


def _stable_read(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    before = _plain_identity(path, directory=False)
    if before.size <= 0 or before.size > maximum_bytes:
        raise ReviewEvidenceError(f"{label} exceeds its supported size limit.")
    try:
        with path.open("rb") as handle:
            opened = _FileIdentity.capture(os.fstat(handle.fileno()))
            raw = handle.read(maximum_bytes + 1)
    except OSError as exc:
        raise ReviewEvidenceError(f"{label} could not be read.") from exc
    after = _plain_identity(path, directory=False)
    if (
        before != after
        or not _same_handle_file_object(before, opened)
        or not _same_handle_file_object(opened, after)
        or len(raw) != before.size
    ):
        raise ReviewEvidenceError(f"{label} changed while it was read.")
    if len(raw) > maximum_bytes:
        raise ReviewEvidenceError(f"{label} exceeds its supported size limit.")
    return raw


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_bytes(value: Any) -> bytes:
    return (_canonical_json(value) + "\n").encode("utf-8")


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ReviewEvidenceError(
            f"{label} must contain exactly: {', '.join(sorted(keys))}."
        )
    return cast(dict[str, Any], value)


def _text(value: Any, label: str, *, maximum: int = MAX_TEXT_LENGTH) -> str:
    if type(value) is not str or not value or len(value) > maximum or "\x00" in value:
        raise ReviewEvidenceError(f"{label} must be bounded non-empty text.")
    return value


def _identifier(value: Any, label: str) -> str:
    rendered = _text(value, label, maximum=200)
    if PurePosixPath(rendered).is_absolute() or PureWindowsPath(rendered).is_absolute():
        raise ReviewEvidenceError(f"{label} cannot contain an absolute path.")
    if rendered.lower().startswith("file:"):
        raise ReviewEvidenceError(f"{label} cannot contain a file URI.")
    return rendered


def _digest(value: Any, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ReviewEvidenceError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _integer(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ReviewEvidenceError(f"{label} is outside the supported integer range.")
    return value


def _timestamp(value: Any) -> str:
    rendered = _text(value, "Record timestamp", maximum=64)
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewEvidenceError("Record timestamp must be ISO-8601 text.") from exc
    if parsed.tzinfo is None or not 2000 <= parsed.year <= 2200:
        raise ReviewEvidenceError("Record timestamp must be timezone-aware and bounded.")
    return rendered


def _is_sensitive_key(key: str) -> bool:
    parts = set(key.split("_"))
    if parts & _SENSITIVE_KEY_PARTS:
        return True
    # Privacy wins over accepting every possible feature label.  These narrow
    # concatenated forms catch common accidental leaks such as ``filepath`` or
    # ``secretvalue``; callers can choose an unambiguous metric name instead.
    return (
        key.startswith(
            (
                "api_key",
                "audio",
                "base64",
                "bearer",
                "credential",
                "file",
                "password",
                "pcm",
                "secret",
                "token",
                "waveform",
            )
        )
        or key.endswith("path")
        or key.endswith("filename")
    )


def _validate_evidence_json(value: Any, label: str) -> None:
    remaining = [MAX_JSON_ITEMS]

    def visit(item: Any, depth: int) -> None:
        if depth > MAX_JSON_DEPTH:
            raise ReviewEvidenceError(f"{label} exceeds the supported nesting depth.")
        remaining[0] -= 1
        if remaining[0] < 0:
            raise ReviewEvidenceError(f"{label} contains too many values.")
        if item is None or type(item) is bool:
            return
        if type(item) is int:
            if not -(1 << 63) <= item <= (1 << 63) - 1:
                raise ReviewEvidenceError(f"{label} contains an out-of-range integer.")
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ReviewEvidenceError(f"{label} contains a non-finite number.")
            return
        if type(item) is str:
            _identifier(item, label)
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for raw_key, child in item.items():
                if type(raw_key) is not str or _SAFE_KEY.fullmatch(raw_key) is None:
                    raise ReviewEvidenceError(f"{label} contains an unsupported object key.")
                if _is_sensitive_key(raw_key):
                    raise ReviewEvidenceError(
                        f"{label} cannot contain private or audio-bearing data."
                    )
                visit(child, depth + 1)
            return
        raise ReviewEvidenceError(f"{label} contains an unsupported JSON value.")

    visit(value, 0)


def validate_review_evidence_record(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and detach one strict, private, non-authoritative record."""

    record = _exact(
        value,
        {
            "schema",
            "authority",
            "category",
            "outcome",
            "recorded_at",
            "project",
            "source",
            "region",
            "feature",
            "proposal",
            "owner_result",
        },
        "Review-evidence record",
    )
    if record["schema"] != RECORD_SCHEMA:
        raise ReviewEvidenceError("Review-evidence record schema is unsupported.")
    if record["authority"] != EVIDENCE_AUTHORITY:
        raise ReviewEvidenceError("Review evidence can never carry approval authority.")
    category = record["category"]
    outcome = record["outcome"]
    if category not in EVIDENCE_CATEGORIES:
        raise ReviewEvidenceError("Review-evidence category is unsupported.")
    if outcome not in OWNER_OUTCOMES:
        raise ReviewEvidenceError("Owner outcome is unsupported.")
    _timestamp(record["recorded_at"])

    project = _exact(
        record["project"],
        {"schema", "sha256", "editable_state_sha256", "source_sha256", "revision"},
        "Project identity",
    )
    _identifier(project["schema"], "Project schema")
    _digest(project["sha256"], "Project SHA-256")
    _digest(project["editable_state_sha256"], "Editable-state SHA-256")
    project_source_sha256 = _digest(project["source_sha256"], "Project source SHA-256")
    _integer(project["revision"], "Project revision", minimum=1, maximum=(1 << 63) - 1)

    source = _exact(
        record["source"],
        {
            "sha256",
            "size_bytes",
            "sample_rate",
            "channels",
            "bits_per_sample",
            "sample_count",
        },
        "Source identity",
    )
    source_sha256 = _digest(source["sha256"], "Source SHA-256")
    if source_sha256 != project_source_sha256:
        raise ReviewEvidenceError("Project and source identities disagree.")
    _integer(source["size_bytes"], "Source size", minimum=1, maximum=(1 << 63) - 1)
    _integer(source["sample_rate"], "Source sample rate", minimum=1, maximum=768_000)
    channel_count = _integer(source["channels"], "Source channels", minimum=1, maximum=64)
    bit_depth = _integer(source["bits_per_sample"], "Source bit depth", minimum=1, maximum=64)
    if bit_depth not in {16, 24, 32}:
        raise ReviewEvidenceError("Source bit depth is unsupported.")
    sample_count = _integer(
        source["sample_count"], "Source sample count", minimum=1, maximum=(1 << 63) - 1
    )

    region = _exact(
        record["region"],
        {"start_frame", "end_frame_exclusive", "channels"},
        "Sample region",
    )
    start = _integer(region["start_frame"], "Region start", minimum=0, maximum=sample_count - 1)
    end = _integer(
        region["end_frame_exclusive"],
        "Region end",
        minimum=1,
        maximum=sample_count,
    )
    if end <= start:
        raise ReviewEvidenceError("Sample region must contain at least one frame.")
    channels = region["channels"]
    if type(channels) is not list or not channels or len(channels) > channel_count:
        raise ReviewEvidenceError("Sample region channels are invalid.")
    validated_channels = [
        _integer(item, "Region channel", minimum=0, maximum=channel_count - 1)
        for item in channels
    ]
    if validated_channels != sorted(set(validated_channels)):
        raise ReviewEvidenceError("Sample region channels must be unique and sorted.")

    feature = _exact(
        record["feature"], {"schema", "tool", "config", "values"}, "Feature evidence"
    )
    _identifier(feature["schema"], "Feature schema")
    tool = _exact(feature["tool"], {"name", "version", "sha256"}, "Feature tool")
    _identifier(tool["name"], "Feature tool name")
    _identifier(tool["version"], "Feature tool version")
    _digest(tool["sha256"], "Feature tool SHA-256")
    config = _exact(feature["config"], {"schema", "sha256"}, "Feature config")
    _identifier(config["schema"], "Feature config schema")
    _digest(config["sha256"], "Feature config SHA-256")
    if type(feature["values"]) is not dict:
        raise ReviewEvidenceError("Feature values must be an object.")
    _validate_evidence_json(feature["values"], "Feature values")

    proposal = _exact(record["proposal"], {"schema", "kind", "payload"}, "Proposal")
    _identifier(proposal["schema"], "Proposal schema")
    _identifier(proposal["kind"], "Proposal kind")
    if type(proposal["payload"]) is not dict:
        raise ReviewEvidenceError("Proposal payload must be an object.")
    _validate_evidence_json(proposal["payload"], "Proposal payload")

    owner_result = _exact(
        record["owner_result"], {"outcome", "schema", "payload"}, "Owner result"
    )
    if owner_result["outcome"] != outcome:
        raise ReviewEvidenceError("Owner result and record outcome disagree.")
    _identifier(owner_result["schema"], "Owner-result schema")
    if type(owner_result["payload"]) is not dict:
        raise ReviewEvidenceError("Owner-result payload must be an object.")
    _validate_evidence_json(owner_result["payload"], "Owner-result payload")

    detached = cast(dict[str, Any], json.loads(_canonical_json(record)))
    raw = _canonical_bytes(detached)
    if len(raw) > MAX_RECORD_BYTES:
        raise ReviewEvidenceError("Review-evidence record exceeds its supported size limit.")
    return detached


def _settings_payload(enabled: bool) -> dict[str, Any]:
    return {
        "schema": SETTINGS_SCHEMA,
        "authority": EVIDENCE_AUTHORITY,
        "enabled": enabled,
    }


def _load_settings(root: Path) -> tuple[bool, bool]:
    path = root / "settings.json"
    if not os.path.lexists(path):
        return False, False
    raw = _stable_read(path, maximum_bytes=4_096, label="Review-evidence settings")
    payload = _decode_json(raw, "Review-evidence settings")
    _exact(payload, {"schema", "authority", "enabled"}, "Review-evidence settings")
    if payload["schema"] != SETTINGS_SCHEMA or payload["authority"] != EVIDENCE_AUTHORITY:
        raise ReviewEvidenceError("Review-evidence settings schema or authority is unsupported.")
    if type(payload["enabled"]) is not bool:
        raise ReviewEvidenceError("Review-evidence enabled setting must be boolean.")
    if raw != _canonical_bytes(payload):
        raise ReviewEvidenceError("Review-evidence settings are not canonical.")
    return payload["enabled"], True


def _write_exact_new(path: Path, raw: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            rename_no_replace(temporary, path)
        except FileExistsError:
            raise
        except OSError as exc:
            raise ReviewEvidenceError(
                "The filesystem cannot atomically create this evidence file without replacement."
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_no_replace_rename(source: Path, destination: Path) -> None:
    """Atomically quarantine one entry without replacing another entry."""

    if source.parent != destination.parent:
        raise ReviewEvidenceError("Evidence quarantine must remain in one directory.")
    try:
        rename_no_replace(source, destination)
    except FileExistsError as exc:
        raise ReviewEvidenceError("Evidence quarantine destination appeared.") from exc
    except (OSError, ValueError) as exc:
        raise ReviewEvidenceError("Evidence quarantine rename failed.") from exc


def set_review_evidence_enabled(root: Path, enabled: bool) -> ReviewEvidenceStatus:
    """Explicitly enable or disable future appends; existing records remain."""

    if type(enabled) is not bool:
        raise ReviewEvidenceError("Review-evidence enabled state must be boolean.")
    root = _absolute(root)
    _ensure_root(root, create=True)
    root_identity = _plain_identity(root, directory=True)
    path = root / "settings.json"
    raw = _canonical_bytes(_settings_payload(enabled))
    existed = os.path.lexists(path)
    if existed:
        _load_settings(root)
    else:
        try:
            _write_exact_new(path, raw)
        except FileExistsError:
            _load_settings(root)
    if os.path.lexists(path):
        existing_identity = _plain_identity(path, directory=False)
        current = _stable_read(path, maximum_bytes=4_096, label="Review-evidence settings")
        if current != raw:
            quarantine = root / (
                f".settings.json.{uuid.uuid4().hex}.quarantine"
            )
            _atomic_no_replace_rename(path, quarantine)
            if not _same_directory_object(
                _plain_identity(root, directory=True), root_identity
            ):
                raise ReviewEvidenceError("Review-evidence root changed during settings update.")
            quarantined_identity = _plain_identity(quarantine, directory=False)
            if not _same_handle_file_object(quarantined_identity, existing_identity):
                raise ReviewEvidenceError("Review-evidence settings were substituted; quarantined.")
            quarantined = _stable_read(
                quarantine,
                maximum_bytes=4_096,
                label="Quarantined review-evidence settings",
            )
            if quarantined != current:
                raise ReviewEvidenceError("Review-evidence settings were substituted; quarantined.")
            try:
                _write_exact_new(path, raw)
            except FileExistsError as exc:
                raise ReviewEvidenceError(
                    "Review-evidence settings path appeared during update; quarantine retained."
                ) from exc
            if _stable_read(
                path,
                maximum_bytes=4_096,
                label="Updated review-evidence settings",
            ) != raw:
                raise ReviewEvidenceError("Updated review-evidence settings failed verification.")
            if (
                not _same_directory_object(
                    _plain_identity(root, directory=True), root_identity
                )
                or _plain_identity(quarantine, directory=False) != quarantined_identity
            ):
                raise ReviewEvidenceError(
                    "Review-evidence settings changed after quarantine; quarantine retained."
                )
            quarantine.unlink()
    return inspect_review_evidence(root)


def _read_record_file(path: Path, expected_sha256: str) -> StoredReviewEvidence:
    raw = _stable_read(path, maximum_bytes=MAX_RECORD_BYTES, label="Review-evidence record")
    observed_sha256 = hashlib.sha256(raw).hexdigest()
    if observed_sha256 != expected_sha256:
        raise ReviewEvidenceError("Review-evidence filename and content hash disagree.")
    payload = validate_review_evidence_record(_decode_json(raw, "Review-evidence record"))
    if raw != _canonical_bytes(payload):
        raise ReviewEvidenceError("Review-evidence record is not canonical.")
    return StoredReviewEvidence(
        expected_sha256,
        path,
        cast(EvidenceCategory, payload["category"]),
        cast(OwnerOutcome, payload["outcome"]),
        cast(str, payload["recorded_at"]),
        _canonical_json(payload),
    )


def list_review_evidence(root: Path) -> tuple[StoredReviewEvidence, ...]:
    """Load and verify every record in deterministic content-hash order."""

    root = _absolute(root)
    if not _ensure_root(root, create=False):
        return ()
    if not _ensure_records_directory(root, create=False):
        return ()
    records_path = root / "records"
    before = _plain_identity(records_path, directory=True)
    try:
        entries = list(records_path.iterdir())
    except OSError as exc:
        raise ReviewEvidenceError("The records directory could not be listed.") from exc
    if _plain_identity(records_path, directory=True) != before:
        raise ReviewEvidenceError("The records directory changed during enumeration.")
    if len(entries) > MAX_RECORDS:
        raise ReviewEvidenceError("The review-evidence store contains too many records.")
    result: list[StoredReviewEvidence] = []
    for path in sorted(entries, key=lambda item: item.name):
        match = _RECORD_NAME.fullmatch(path.name)
        if match is None:
            raise ReviewEvidenceError("The records directory contains an unexpected entry.")
        result.append(_read_record_file(path, match.group(1)))
    if _plain_identity(records_path, directory=True) != before:
        raise ReviewEvidenceError("The records directory changed while it was inspected.")
    return tuple(result)


def inspect_review_evidence(root: Path) -> ReviewEvidenceStatus:
    """Inspect settings and verify the complete local inventory."""

    root = _absolute(root)
    if not _ensure_root(root, create=False):
        return ReviewEvidenceStatus(root, False, False, 0)
    enabled, configured = _load_settings(root)
    records = list_review_evidence(root)
    return ReviewEvidenceStatus(root, enabled, configured, len(records))


def append_review_evidence(root: Path, value: Mapping[str, Any]) -> StoredReviewEvidence:
    """Append one content-addressed record only when collection is enabled."""

    root = _absolute(root)
    _ensure_root(root, create=True)
    enabled, _configured = _load_settings(root)
    if not enabled:
        raise ReviewEvidenceError(
            "Review-evidence collection is disabled; explicitly enable it before appending."
        )
    _ensure_records_directory(root, create=True)
    if len(list_review_evidence(root)) >= MAX_RECORDS:
        raise ReviewEvidenceError("The review-evidence store reached its record limit.")
    payload = validate_review_evidence_record(value)
    raw = _canonical_bytes(payload)
    record_sha256 = hashlib.sha256(raw).hexdigest()
    target = root / "records" / f"{record_sha256}.json"
    if os.path.lexists(target):
        return _read_record_file(target, record_sha256)
    try:
        _write_exact_new(target, raw)
    except FileExistsError:
        pass
    return _read_record_file(target, record_sha256)


def build_review_evidence_export(root: Path) -> dict[str, Any]:
    """Build a deterministic, path-free, audio-free verified export."""

    records = list_review_evidence(root)
    payload = {
        "schema": EXPORT_SCHEMA,
        "authority": EVIDENCE_AUTHORITY,
        "record_count": len(records),
        "records": [
            {"record_sha256": item.record_sha256, "record": item.to_dict()}
            for item in records
        ],
    }
    if len(_canonical_bytes(payload)) > MAX_EXPORT_BYTES:
        raise ReviewEvidenceError("Review-evidence export exceeds its supported size limit.")
    return payload


def validate_review_evidence_export(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a portable export and all content-derived record identities."""

    export = _exact(
        value, {"schema", "authority", "record_count", "records"}, "Review-evidence export"
    )
    if export["schema"] != EXPORT_SCHEMA or export["authority"] != EVIDENCE_AUTHORITY:
        raise ReviewEvidenceError("Review-evidence export schema or authority is unsupported.")
    count = _integer(export["record_count"], "Export record count", minimum=0, maximum=MAX_RECORDS)
    records = export["records"]
    if type(records) is not list or len(records) != count:
        raise ReviewEvidenceError("Review-evidence export count is inconsistent.")
    previous = ""
    for raw_entry in records:
        entry = _exact(raw_entry, {"record_sha256", "record"}, "Export record entry")
        expected = _digest(entry["record_sha256"], "Export record SHA-256")
        if expected <= previous:
            raise ReviewEvidenceError("Export records must be unique and hash-sorted.")
        record = validate_review_evidence_record(entry["record"])
        if hashlib.sha256(_canonical_bytes(record)).hexdigest() != expected:
            raise ReviewEvidenceError("Export record identity does not match its content.")
        previous = expected
    if len(_canonical_bytes(export)) > MAX_EXPORT_BYTES:
        raise ReviewEvidenceError("Review-evidence export exceeds its supported size limit.")
    return cast(dict[str, Any], json.loads(_canonical_json(export)))


def export_review_evidence(root: Path, output_path: Path) -> str:
    """Write one deterministic export to a new non-reparse file."""

    output_path = _absolute(output_path)
    if os.path.lexists(output_path):
        raise ReviewEvidenceError("Review-evidence export destination must not exist.")
    if not output_path.parent.exists():
        raise ReviewEvidenceError("Review-evidence export parent does not exist.")
    _plain_identity(output_path.parent, directory=True)
    raw = _canonical_bytes(build_review_evidence_export(root))
    try:
        _write_exact_new(output_path, raw)
    except FileExistsError as exc:
        raise ReviewEvidenceError("Review-evidence export destination appeared.") from exc
    return hashlib.sha256(raw).hexdigest()


def load_review_evidence_export(path: Path) -> dict[str, Any]:
    """Reopen and completely validate one portable export."""

    path = _absolute(path)
    raw = _stable_read(path, maximum_bytes=MAX_EXPORT_BYTES, label="Review-evidence export")
    payload = validate_review_evidence_export(_decode_json(raw, "Review-evidence export"))
    if raw != _canonical_bytes(payload):
        raise ReviewEvidenceError("Review-evidence export is not canonical.")
    return payload


def delete_review_evidence(
    root: Path,
    record_sha256: str,
    *,
    expected_record_sha256: str,
    deliberate: bool,
) -> StoredReviewEvidence:
    """Delete exactly one verified record after redundant deliberate confirmation."""

    record_sha256 = _digest(record_sha256, "Record SHA-256")
    expected_record_sha256 = _digest(expected_record_sha256, "Expected record SHA-256")
    if not deliberate:
        raise ReviewEvidenceError("Record deletion requires deliberate confirmation.")
    if record_sha256 != expected_record_sha256:
        raise ReviewEvidenceError("Record and expected SHA-256 values must match exactly.")
    root = _absolute(root)
    if not _ensure_root(root, create=False) or not _ensure_records_directory(root, create=False):
        raise ReviewEvidenceError("Review-evidence record does not exist.")
    records_path = root / "records"
    directory_identity = _plain_identity(records_path, directory=True)
    target = records_path / f"{record_sha256}.json"
    if not os.path.lexists(target):
        raise ReviewEvidenceError("Review-evidence record does not exist.")
    before = _plain_identity(target, directory=False)
    _read_record_file(target, record_sha256)
    if _plain_identity(target, directory=False) != before:
        raise ReviewEvidenceError("Review-evidence record changed before deletion.")
    quarantine = records_path / (
        f".{record_sha256}.{uuid.uuid4().hex}.delete-quarantine"
    )
    _atomic_no_replace_rename(target, quarantine)
    if not _same_directory_object(
        _plain_identity(records_path, directory=True), directory_identity
    ):
        raise ReviewEvidenceError(
            "The records directory was substituted; quarantined entry was not removed."
        )
    try:
        quarantined_identity = _plain_identity(quarantine, directory=False)
    except ReviewEvidenceError as exc:
        raise ReviewEvidenceError(
            "The record was substituted during deletion; quarantined entry was not removed."
        ) from exc
    if not _same_handle_file_object(quarantined_identity, before):
        raise ReviewEvidenceError(
            "The record was substituted during deletion; quarantined entry was not removed."
        )
    record = _read_record_file(quarantine, record_sha256)
    if (
        _plain_identity(quarantine, directory=False) != quarantined_identity
        or not _same_directory_object(
            _plain_identity(records_path, directory=True), directory_identity
        )
    ):
        raise ReviewEvidenceError(
            "The quarantined record changed before removal; it was not removed."
        )
    try:
        quarantine.unlink()
    except OSError as exc:
        raise ReviewEvidenceError("Review-evidence record could not be deleted.") from exc
    if os.path.lexists(target):
        raise ReviewEvidenceError(
            "A record appeared again during deletion; the new entry was left untouched."
        )
    return record


__all__ = [
    "EVIDENCE_AUTHORITY",
    "EVIDENCE_CATEGORIES",
    "EXPORT_SCHEMA",
    "EvidenceCategory",
    "MAX_EXPORT_BYTES",
    "MAX_RECORD_BYTES",
    "MAX_RECORDS",
    "OWNER_OUTCOMES",
    "OwnerOutcome",
    "RECORD_SCHEMA",
    "ReviewEvidenceError",
    "ReviewEvidenceStatus",
    "SETTINGS_SCHEMA",
    "StoredReviewEvidence",
    "append_review_evidence",
    "build_review_evidence_export",
    "default_review_evidence_root",
    "delete_review_evidence",
    "export_review_evidence",
    "inspect_review_evidence",
    "list_review_evidence",
    "load_review_evidence_export",
    "resolve_review_evidence_root",
    "review_evidence_may_apply_action",
    "review_evidence_may_authorize_action",
    "set_review_evidence_enabled",
    "validate_review_evidence_export",
    "validate_review_evidence_record",
]
