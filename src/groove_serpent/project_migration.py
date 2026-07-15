from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

from .atomic_create import rename_no_replace
from .errors import ProjectValidationError
from .migration_commit import (
    prepare_replacement,
    quarantine_path_no_replace,
    remove_exact_plain_file,
)
from .models import (
    ANALYZER_BASELINE_SCHEMA,
    PROJECT_STATE_SCHEMA,
    SCHEMA_VERSION,
    Project,
    utc_now_iso,
)
from .project_io import (
    MAX_PROJECT_FILE_BYTES,
    decode_project_json,
)
from .transaction_lock import (
    TargetWriteLease,
    canonical_target_path,
    exclusive_target_write_lease,
)

MIGRATION_PLAN_SCHEMA = "groove-serpent.project-migration-plan/1"
MIGRATION_PENDING_SCHEMA = "groove-serpent.project-migration-pending/1"
MIGRATION_RECEIPT_SCHEMA = "groove-serpent.project-migration-receipt/1"
MAX_MIGRATION_AUX_BYTES = 1024 * 1024

_BASE_ROOT_FIELDS = {
    "source",
    "settings",
    "analysis",
    "tracks",
    "metadata",
    "schema_version",
    "app_version",
    "created_at",
    "updated_at",
}
_V3_STATE_FIELDS = {
    "analyzer_baseline",
    "edit_history",
    "checkpoints",
    "revision",
}
_V1_SOURCE_FIELDS = {
    "path",
    "filename",
    "size_bytes",
    "modified_ns",
    "duration_seconds",
    "sample_rate",
    "channels",
    "codec_name",
    "bits_per_raw_sample",
    "sample_format",
}
_CURRENT_SOURCE_FIELDS = _V1_SOURCE_FIELDS | {"sample_count", "sha256"}
_SETTINGS_FIELDS = {
    "analysis_rate",
    "window_ms",
    "smoothing_windows",
    "threshold_margin_db",
    "min_gap_seconds",
    "max_gap_seconds",
    "min_track_seconds",
    "active_run_seconds",
    "lead_in_seconds",
    "tail_seconds",
    "auto_boundary_score",
    "waveform_points",
}
_ANALYSIS_FIELDS = {
    "music_start_seconds",
    "music_end_seconds",
    "noise_floor_db",
    "silence_threshold_db",
    "active_threshold_db",
    "envelope_window_seconds",
    "candidates",
    "waveform",
}
_CANDIDATE_FIELDS = {
    "start_seconds",
    "end_seconds",
    "cut_seconds",
    "cut_sample",
    "duration_seconds",
    "minimum_db",
    "mean_db",
    "contrast_db",
    "score",
    "selected",
}
_V1_TRACK_FIELDS = {
    "number",
    "title",
    "start_sample",
    "end_sample",
    "start_seconds",
    "end_seconds",
    "confidence",
    "artist",
    "album",
    "album_artist",
    "year",
    "genre",
    "side",
    "expected_duration_seconds",
}
_CURRENT_TRACK_FIELDS = _V1_TRACK_FIELDS | {
    "musicbrainz_recording_id",
    "musicbrainz_track_id",
}


@dataclass(frozen=True, slots=True)
class MigrationArtifactPaths:
    backup: Path
    candidate: Path
    pending: Path
    receipt: Path


@dataclass(frozen=True, slots=True)
class ProjectMigrationResult:
    status: Literal["current", "migrated", "recovered"]
    project: str
    original_schema: int
    target_schema: int
    original_sha256: str
    migrated_sha256: str
    backup: str | None
    receipt: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "groove-serpent.project-migration-result/1",
            "status": self.status,
            "project": self.project,
            "original_schema": self.original_schema,
            "target_schema": self.target_schema,
            "original_sha256": self.original_sha256,
            "migrated_sha256": self.migrated_sha256,
            "backup": self.backup,
            "receipt": self.receipt,
        }


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int

    @classmethod
    def capture(cls, value: os.stat_result) -> "_FileSnapshot":
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            mode=value.st_mode,
            size=value.st_size,
            modified_ns=value.st_mtime_ns,
        )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ProjectValidationError(
            f"Migration data is not canonical finite JSON: {exc}"
        ) from exc
    return rendered.encode("utf-8")


def _pretty_json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProjectValidationError(
            f"Migration data is not finite JSON: {exc}"
        ) from exc


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in value
    ):
        raise ProjectValidationError(f"{label} must be a JSON object with text keys.")
    return value


def _require_exact_keys(
    value: Any,
    expected: set[str],
    label: str,
    *,
    optional: set[str] | None = None,
) -> dict[str, Any]:
    data = _require_mapping(value, label)
    optional = optional or set()
    missing = expected - data.keys()
    unexpected = data.keys() - expected - optional
    if missing:
        raise ProjectValidationError(
            f"{label} is missing field(s): {', '.join(sorted(missing))}."
        )
    if unexpected:
        raise ProjectValidationError(
            f"{label} contains unexpected field(s): "
            f"{', '.join(sorted(unexpected))}."
        )
    return data


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProjectValidationError(f"{label} must be a JSON array.")
    return value


def _validate_archive_shape(data: dict[str, Any], schema_version: int) -> None:
    if schema_version in {1, 2}:
        _require_exact_keys(data, _BASE_ROOT_FIELDS, f"Schema {schema_version} project")
    elif schema_version == 3:
        _require_exact_keys(
            data,
            _BASE_ROOT_FIELDS,
            "Schema 3 project",
            optional=_V3_STATE_FIELDS,
        )
    else:
        raise ProjectValidationError(
            f"Project schema {schema_version} cannot be migrated to {SCHEMA_VERSION}."
        )

    source_fields = _V1_SOURCE_FIELDS if schema_version == 1 else _CURRENT_SOURCE_FIELDS
    _require_exact_keys(data["source"], source_fields, "Legacy audio source")
    _require_exact_keys(data["settings"], _SETTINGS_FIELDS, "Legacy analysis settings")
    analysis = _require_exact_keys(
        data["analysis"], _ANALYSIS_FIELDS, "Legacy analysis summary"
    )
    for index, candidate in enumerate(
        _require_list(analysis["candidates"], "Legacy boundary candidates"),
        start=1,
    ):
        _require_exact_keys(
            candidate,
            _CANDIDATE_FIELDS,
            f"Legacy boundary candidate {index}",
        )
    _require_list(analysis["waveform"], "Legacy analysis waveform")
    track_fields = _V1_TRACK_FIELDS if schema_version == 1 else _CURRENT_TRACK_FIELDS
    for index, track in enumerate(
        _require_list(data["tracks"], "Legacy project tracks"), start=1
    ):
        _require_exact_keys(track, track_fields, f"Legacy track {index}")
    _require_mapping(data["metadata"], "Legacy project metadata")


def _state_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": PROJECT_STATE_SCHEMA,
        "tracks": copy.deepcopy(data["tracks"]),
        "metadata": copy.deepcopy(data["metadata"]),
    }


def _baseline_payload(data: dict[str, Any]) -> dict[str, Any]:
    state = _state_payload(data)
    source = _require_mapping(data["source"], "Legacy audio source")
    source_sha256 = source.get("sha256", "")
    if not isinstance(source_sha256, str):
        raise ProjectValidationError("Legacy source SHA-256 must be text.")
    return {
        "state": state,
        "state_sha256": _sha256(_canonical_json_bytes(state)),
        "source_sha256": source_sha256.lower(),
        "schema": ANALYZER_BASELINE_SCHEMA,
    }


def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    _validate_archive_shape(data, 1)
    migrated = copy.deepcopy(data)
    source = _require_mapping(migrated["source"], "Legacy audio source")
    source["sample_count"] = None
    source["sha256"] = ""
    for track in _require_list(migrated["tracks"], "Legacy project tracks"):
        track_data = _require_mapping(track, "Legacy track")
        track_data["musicbrainz_recording_id"] = ""
        track_data["musicbrainz_track_id"] = ""
    migrated["schema_version"] = 2
    return migrated


def _migrate_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    _validate_archive_shape(data, 2)
    migrated = copy.deepcopy(data)
    migrated["analyzer_baseline"] = _baseline_payload(migrated)
    migrated["edit_history"] = []
    migrated["checkpoints"] = []
    migrated["revision"] = 1
    migrated["schema_version"] = 3
    return migrated


def _migrate_v3_to_v4(data: dict[str, Any]) -> dict[str, Any]:
    _validate_archive_shape(data, 3)
    migrated = copy.deepcopy(data)
    if migrated.get("analyzer_baseline") is None:
        migrated["analyzer_baseline"] = _baseline_payload(migrated)
    migrated.setdefault("edit_history", [])
    migrated.setdefault("checkpoints", [])
    migrated.setdefault("revision", 1)
    migrated["schema_version"] = SCHEMA_VERSION
    project = Project.from_dict(migrated)
    return project.to_dict()


_MIGRATORS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
}


def migrate_project_data(data: dict[str, Any]) -> tuple[Project, tuple[str, ...]]:
    """Apply every registered migration sequentially and validate schema 4."""

    working = copy.deepcopy(_require_mapping(data, "Project"))
    schema_value = working.get("schema_version")
    if type(schema_value) is not int:
        raise ProjectValidationError("The project schema version must be an integer.")
    if schema_value > SCHEMA_VERSION:
        raise ProjectValidationError(
            f"Project schema {schema_value} is newer than supported schema "
            f"{SCHEMA_VERSION}; it cannot be downgraded."
        )
    if schema_value < 1:
        raise ProjectValidationError(
            f"Unsupported project schema {schema_value}; expected 1-{SCHEMA_VERSION}."
        )
    steps: list[str] = []
    while schema_value < SCHEMA_VERSION:
        migrator = _MIGRATORS.get(schema_value)
        if migrator is None:
            raise ProjectValidationError(
                f"No migration is registered from project schema {schema_value}."
            )
        working = migrator(working)
        next_schema = working.get("schema_version")
        if type(next_schema) is not int or next_schema != schema_value + 1:
            raise ProjectValidationError("The project migration registry is not sequential.")
        steps.append(f"{schema_value}->{next_schema}")
        schema_value = next_schema
    return Project.from_dict(working), tuple(steps)


def _is_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _assert_plain_file(path: Path) -> os.stat_result:
    try:
        value = path.lstat()
    except FileNotFoundError:
        raise
    if (
        path.is_symlink()
        or _is_reparse(value)
        or not stat.S_ISREG(value.st_mode)
        or int(value.st_nlink) != 1
    ):
        raise ProjectValidationError(
            "Migration refuses linked, non-regular, or reparse-point file: "
            f"{path.name}"
        )
    return value


def _assert_plain_parent(path: Path) -> _FileSnapshot:
    parent = path.parent
    value = parent.lstat()
    if parent.is_symlink() or _is_reparse(value) or not stat.S_ISDIR(value.st_mode):
        raise ProjectValidationError(
            "Project migration requires a regular, non-reparse parent directory."
        )
    return _FileSnapshot.capture(value)


def _read_snapshot(
    path: Path, *, maximum: int | None = None
) -> tuple[bytes, _FileSnapshot]:
    if maximum is None:
        maximum = MAX_PROJECT_FILE_BYTES
    before = _FileSnapshot.capture(_assert_plain_file(path))
    with path.open("rb") as handle:
        opened = _FileSnapshot.capture(os.fstat(handle.fileno()))
        raw = handle.read(maximum + 1)
    after = _FileSnapshot.capture(_assert_plain_file(path))
    if before != opened or opened != after:
        raise ProjectValidationError(
            f"File identity changed while reading {path.name}; migration stopped."
        )
    if len(raw) > maximum:
        raise ProjectValidationError(
            f"{path.name} exceeds the {maximum}-byte migration limit."
        )
    return raw, after


def _replace_sibling(source: Path, destination: Path) -> None:
    """Replace within one verified parent, using a directory fd on POSIX."""

    if source.parent != destination.parent:
        raise ProjectValidationError(
            "Migration replacement source and target must share one parent."
        )
    if os.name != "nt" and os.rename in os.supports_dir_fd:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(destination.parent, flags)
        try:
            opened = _FileSnapshot.capture(os.fstat(descriptor))
            if opened != _assert_plain_parent(destination):
                raise ProjectValidationError(
                    "Project migration parent identity changed before replacement."
                )
            os.replace(
                source.name,
                destination.name,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
        finally:
            os.close(descriptor)
        return
    os.replace(source, destination)


def _restore_project_backup(
    path: Path,
    backup_raw: bytes,
    original_sha256: str,
    write_lease: TargetWriteLease,
) -> None:
    """Restore exact bytes without overwriting an unowned path replacement."""

    try:
        current_raw, _ = _read_snapshot(path)
    except (FileNotFoundError, ProjectValidationError):
        current_raw = b""
    if current_raw == backup_raw and _sha256(current_raw) == original_sha256:
        return
    last_error: BaseException | None = None
    preserved: list[Path] = []
    for _ in range(3):
        if _lexists(path):
            try:
                write_lease.assert_current()
                preserved.append(
                    quarantine_path_no_replace(
                        path, purpose="migration-rollback-conflict"
                    )
                )
            except BaseException as exc:
                last_error = exc
                continue
        prepared = prepare_replacement(
            path,
            backup_raw,
            maximum=MAX_PROJECT_FILE_BYTES,
            purpose="migration-rollback",
        )
        try:
            write_lease.assert_current()
            try:
                rename_no_replace(prepared.path, path)
            except FileExistsError as exc:
                last_error = exc
                continue
            except BaseException as exc:
                last_error = exc
            if prepared.matches_target(path):
                restored_raw, _ = _read_snapshot(path)
                if (
                    restored_raw == backup_raw
                    and _sha256(restored_raw) == original_sha256
                ):
                    return
            last_error = ProjectValidationError(
                "Migration rollback did not install the verified backup."
            )
        finally:
            prepared.discard()
    raise ProjectValidationError(
        "Migration detected an unsafe post-commit mismatch and could not "
        "restore the verified backup without overwriting a conflict. "
        f"Preserved: {', '.join(item.name for item in preserved) or 'none'}. "
        f"Last error: {last_error}"
    ) from last_error


def _commit_project_candidate(
    path: Path,
    candidate: Path,
    candidate_raw: bytes,
    migrated_sha256: str,
    backup_raw: bytes,
    original_sha256: str,
    write_lease: TargetWriteLease,
) -> None:
    """Commit trusted bytes without treating the candidate pathname as authority."""

    prepared = prepare_replacement(
        path,
        candidate_raw,
        maximum=MAX_PROJECT_FILE_BYTES,
        purpose="migration-commit",
    )
    try:
        write_lease.assert_current()
        try:
            _replace_sibling(prepared.path, path)
        except BaseException:
            if not prepared.matches_target(path):
                _restore_project_backup(
                    path, backup_raw, original_sha256, write_lease
                )
            raise
        if not prepared.matches_target(path):
            _restore_project_backup(path, backup_raw, original_sha256, write_lease)
            raise ProjectValidationError(
                "Migration replacement did not install the descriptor-bound "
                "candidate; the verified backup was restored."
            )
        try:
            write_lease.assert_current()
            _unlink_verified(candidate, migrated_sha256, MAX_PROJECT_FILE_BYTES)
        except BaseException as exc:
            _restore_project_backup(path, backup_raw, original_sha256, write_lease)
            raise ProjectValidationError(
                "Migration candidate identity changed at commit; the verified "
                "backup was restored."
            ) from exc
        if _lexists(candidate):
            _restore_project_backup(path, backup_raw, original_sha256, write_lease)
            raise ProjectValidationError(
                "Migration candidate reappeared during commit; the verified "
                "backup was restored."
            )
    finally:
        prepared.discard()


# The side-effect-free validation pass is intentionally a distinct read phase.
# Keeping its callable stable also lets transaction-race instrumentation target
# only the authoritative under-lease reads.
_read_preflight_snapshot = _read_snapshot


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _write_exclusive(path: Path, payload: bytes) -> None:
    if _lexists(path):
        raise ProjectValidationError(
            f"Migration artifact already exists and will not be overwritten: {path.name}"
        )
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ProjectValidationError(
            f"Migration artifact already exists and will not be overwritten: {path.name}"
        ) from exc


def _unlink_verified(path: Path, expected_sha256: str, maximum: int) -> None:
    remove_exact_plain_file(
        path,
        expected_sha256,
        maximum=maximum,
        purpose="migration-artifact-cleanup",
    )


def migration_artifact_paths(
    project_path: Path, original_sha256: str, original_schema: int
) -> MigrationArtifactPaths:
    """Return deterministic bounded sibling names for one exact source file."""

    project_path = canonical_target_path(project_path)
    filename_id = _sha256(project_path.name.encode("utf-8"))[:12]
    base = (
        f".groove-serpent-migration-{filename_id}-"
        f"{original_sha256[:16]}-v{original_schema}-v{SCHEMA_VERSION}"
    )
    return MigrationArtifactPaths(
        backup=project_path.parent / f"{base}.backup",
        candidate=project_path.parent / f"{base}.candidate",
        pending=project_path.parent / f"{base}.pending.json",
        receipt=project_path.parent / f"{base}.receipt.json",
    )


def _project_payload_bytes(project: Project) -> bytes:
    payload = _pretty_json_bytes(project.to_dict())
    reparsed = Project.from_dict(decode_project_json(payload))
    if reparsed.to_dict() != project.to_dict():
        raise ProjectValidationError(
            "Migrated project did not reproduce after canonical serialization."
        )
    return payload


def _expected_steps(original_schema: int) -> list[str]:
    return [f"{value}->{value + 1}" for value in range(original_schema, SCHEMA_VERSION)]


def _build_plan(
    *,
    path: Path,
    original_data: dict[str, Any],
    original_schema: int,
    original_sha256: str,
    migrated_project: Project,
    migrated_sha256: str,
    artifacts: MigrationArtifactPaths,
    steps: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "schema": MIGRATION_PLAN_SCHEMA,
        "project": path.name,
        "project_filename_sha256": _sha256(path.name.encode("utf-8")),
        "original_schema": original_schema,
        "target_schema": SCHEMA_VERSION,
        "steps": list(steps),
        "original_sha256": original_sha256,
        "migrated_sha256": migrated_sha256,
        "source_sha256": migrated_project.source.sha256.lower(),
        "editable_state_sha256": migrated_project.state_sha256,
        "backup": artifacts.backup.name,
        "candidate": artifacts.candidate.name,
        "receipt": artifacts.receipt.name,
        "original_provenance": {
            "app_version": original_data["app_version"],
            "created_at": original_data["created_at"],
            "updated_at": original_data["updated_at"],
        },
    }


def _validate_digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProjectValidationError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _validate_plan(
    value: Any, path: Path
) -> tuple[dict[str, Any], MigrationArtifactPaths]:
    fields = {
        "schema",
        "project",
        "project_filename_sha256",
        "original_schema",
        "target_schema",
        "steps",
        "original_sha256",
        "migrated_sha256",
        "source_sha256",
        "editable_state_sha256",
        "backup",
        "candidate",
        "receipt",
        "original_provenance",
    }
    plan = _require_exact_keys(value, fields, "Migration plan")
    if plan["schema"] != MIGRATION_PLAN_SCHEMA or plan["project"] != path.name:
        raise ProjectValidationError("Migration plan targets a different project.")
    if plan["project_filename_sha256"] != _sha256(path.name.encode("utf-8")):
        raise ProjectValidationError("Migration plan project identity does not match.")
    original_schema = plan["original_schema"]
    if type(original_schema) is not int or original_schema not in {1, 2, 3}:
        raise ProjectValidationError("Migration plan has an invalid original schema.")
    if plan["target_schema"] != SCHEMA_VERSION:
        raise ProjectValidationError("Migration plan has an invalid target schema.")
    if plan["steps"] != _expected_steps(original_schema):
        raise ProjectValidationError("Migration plan steps are not sequential.")
    original_sha256 = _validate_digest(
        plan["original_sha256"], "Migration original SHA-256"
    )
    _validate_digest(plan["migrated_sha256"], "Migration result SHA-256")
    source_sha256 = plan["source_sha256"]
    if source_sha256 != "":
        _validate_digest(source_sha256, "Migration source SHA-256")
    _validate_digest(
        plan["editable_state_sha256"], "Migration editable-state SHA-256"
    )
    provenance = _require_exact_keys(
        plan["original_provenance"],
        {"app_version", "created_at", "updated_at"},
        "Migration provenance",
    )
    if any(not isinstance(provenance[key], str) for key in provenance):
        raise ProjectValidationError("Migration provenance values must be text.")
    expected = migration_artifact_paths(path, original_sha256, original_schema)
    for key, expected_path in (
        ("backup", expected.backup),
        ("candidate", expected.candidate),
        ("receipt", expected.receipt),
    ):
        value_name = plan[key]
        if (
            not isinstance(value_name, str)
            or Path(value_name).name != value_name
            or value_name != expected_path.name
        ):
            raise ProjectValidationError(
                f"Migration plan {key} path is not the expected portable sibling."
            )
    return plan, expected


def _pending_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": MIGRATION_PENDING_SCHEMA,
        "plan": plan,
        "plan_sha256": _sha256(_canonical_json_bytes(plan)),
    }


def _read_aux_json(path: Path) -> tuple[dict[str, Any], bytes]:
    raw, _ = _read_snapshot(path, maximum=MAX_MIGRATION_AUX_BYTES)
    try:
        return decode_project_json(raw), raw
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectValidationError(
            f"Migration artifact {path.name} is invalid JSON: {exc}"
        ) from exc


def _read_pending(
    path: Path, pending_path: Path
) -> tuple[dict[str, Any], MigrationArtifactPaths, str]:
    payload, raw = _read_aux_json(pending_path)
    _require_exact_keys(
        payload,
        {"schema", "plan", "plan_sha256"},
        "Pending migration journal",
    )
    if payload["schema"] != MIGRATION_PENDING_SCHEMA:
        raise ProjectValidationError("Pending migration journal schema is invalid.")
    plan, artifacts = _validate_plan(payload["plan"], path)
    plan_sha256 = _validate_digest(
        payload["plan_sha256"], "Pending migration plan SHA-256"
    )
    if plan_sha256 != _sha256(_canonical_json_bytes(plan)):
        raise ProjectValidationError("Pending migration plan hash does not match.")
    if artifacts.pending != pending_path:
        raise ProjectValidationError("Pending migration journal name is inconsistent.")
    return plan, artifacts, _sha256(raw)


def _find_pending(path: Path) -> Path | None:
    filename_id = _sha256(path.name.encode("utf-8"))[:12]
    match: Path | None = None
    for candidate in path.parent.glob(
        f".groove-serpent-migration-{filename_id}-*.pending.json"
    ):
        if match is not None:
            raise ProjectValidationError(
                "Multiple pending migration journals target this project; "
                "inspect them manually."
            )
        match = candidate
    return match


def _validate_receipt(
    receipt_path: Path, expected_plan: dict[str, Any], expected_plan_sha256: str
) -> None:
    receipt, _ = _read_aux_json(receipt_path)
    _require_exact_keys(
        receipt,
        {"schema", "status", "plan", "plan_sha256", "committed_at"},
        "Migration receipt",
    )
    committed_at = receipt["committed_at"]
    if (
        receipt["schema"] != MIGRATION_RECEIPT_SCHEMA
        or receipt["status"] != "committed"
        or receipt["plan"] != expected_plan
        or receipt["plan_sha256"] != expected_plan_sha256
    ):
        raise ProjectValidationError("Existing migration receipt does not match the plan.")
    if not isinstance(committed_at, str) or not committed_at or len(committed_at) > 64:
        raise ProjectValidationError(
            "Migration receipt commit time must be bounded ISO-8601 text."
        )
    try:
        parsed = datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectValidationError(
            "Migration receipt commit time must be valid ISO-8601 text."
        ) from exc
    if parsed.tzinfo is None:
        raise ProjectValidationError(
            "Migration receipt commit time must include a timezone."
        )


def _result_from_plan(
    plan: dict[str, Any], status: Literal["migrated", "recovered"]
) -> ProjectMigrationResult:
    return ProjectMigrationResult(
        status=status,
        project=str(plan["project"]),
        original_schema=int(plan["original_schema"]),
        target_schema=SCHEMA_VERSION,
        original_sha256=str(plan["original_sha256"]),
        migrated_sha256=str(plan["migrated_sha256"]),
        backup=str(plan["backup"]),
        receipt=str(plan["receipt"]),
    )


def _resume_pending(
    path: Path,
    pending_path: Path,
    *,
    newly_prepared: bool,
    write_lease: TargetWriteLease,
) -> ProjectMigrationResult:
    plan, artifacts, pending_sha256 = _read_pending(path, pending_path)
    original_sha256 = str(plan["original_sha256"])
    migrated_sha256 = str(plan["migrated_sha256"])

    backup_raw, _ = _read_snapshot(artifacts.backup)
    if _sha256(backup_raw) != original_sha256:
        raise ProjectValidationError("Migration backup no longer matches the original hash.")

    parent_snapshot = _assert_plain_parent(path)
    current_raw, current_snapshot = _read_snapshot(path)
    current_sha256 = _sha256(current_raw)
    if current_sha256 == original_sha256:
        if _lexists(artifacts.receipt):
            raise ProjectValidationError(
                "A committed receipt exists while the original project is still "
                "present; the inconsistent transaction was left untouched."
            )
        candidate_raw, _ = _read_snapshot(artifacts.candidate)
        if _sha256(candidate_raw) != migrated_sha256:
            raise ProjectValidationError("Migration candidate hash does not match the plan.")
        candidate_project = Project.from_dict(decode_project_json(candidate_raw))
        if (
            candidate_project.state_sha256 != plan["editable_state_sha256"]
            or candidate_project.source.sha256.lower() != plan["source_sha256"]
        ):
            raise ProjectValidationError("Migration candidate identity does not match the plan.")
        repeated_raw, repeated_snapshot = _read_snapshot(path)
        if (
            repeated_snapshot != current_snapshot
            or _sha256(repeated_raw) != original_sha256
            or _assert_plain_parent(path) != parent_snapshot
        ):
            raise ProjectValidationError(
                "Project or parent identity changed before atomic replacement."
            )
        _commit_project_candidate(
            path,
            artifacts.candidate,
            candidate_raw,
            migrated_sha256,
            backup_raw,
            original_sha256,
            write_lease,
        )
        current_raw, _ = _read_snapshot(path)
        current_sha256 = _sha256(current_raw)

    if current_sha256 != migrated_sha256:
        raise ProjectValidationError(
            "Pending migration conflicts with the current project bytes; no file was changed."
        )
    if _lexists(artifacts.candidate):
        candidate_raw, _ = _read_snapshot(artifacts.candidate)
        if _sha256(candidate_raw) != migrated_sha256:
            raise ProjectValidationError(
                "The migrated project and a changed candidate both exist; the "
                "inconsistent transaction was left untouched."
            )
        write_lease.assert_current()
        _unlink_verified(
            artifacts.candidate,
            migrated_sha256,
            MAX_PROJECT_FILE_BYTES,
        )
    migrated_project = Project.from_dict(decode_project_json(current_raw))
    if (
        migrated_project.state_sha256 != plan["editable_state_sha256"]
        or migrated_project.source.sha256.lower() != plan["source_sha256"]
    ):
        raise ProjectValidationError("Migrated project identity does not match the plan.")

    plan_sha256 = _sha256(_canonical_json_bytes(plan))
    if _lexists(artifacts.receipt):
        _validate_receipt(artifacts.receipt, plan, plan_sha256)
    else:
        receipt = {
            "schema": MIGRATION_RECEIPT_SCHEMA,
            "status": "committed",
            "plan": plan,
            "plan_sha256": plan_sha256,
            "committed_at": utc_now_iso(),
        }
        write_lease.assert_current()
        _write_exclusive(artifacts.receipt, _pretty_json_bytes(receipt))

    write_lease.assert_current()
    _unlink_verified(pending_path, pending_sha256, MAX_MIGRATION_AUX_BYTES)
    status: Literal["migrated", "recovered"] = (
        "migrated" if newly_prepared else "recovered"
    )
    return _result_from_plan(plan, status)


def _migrate_project_file_transaction(
    path: Path,
    write_lease: TargetWriteLease | None,
    *,
    prepare_only: bool,
) -> ProjectMigrationResult | None:
    """Validate once without writes, or run with the target lease held."""

    _assert_plain_parent(path)
    pending_path = _find_pending(path)
    if pending_path is not None:
        if prepare_only:
            return None
        if write_lease is None:
            raise ProjectValidationError("Project migration write lease is missing.")
        return _resume_pending(
            path,
            pending_path,
            newly_prepared=False,
            write_lease=write_lease,
        )

    snapshot_reader = _read_preflight_snapshot if prepare_only else _read_snapshot
    original_raw, _ = snapshot_reader(path)
    original_sha256 = _sha256(original_raw)
    try:
        original_data = decode_project_json(original_raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectValidationError(f"Project file is invalid: {exc}") from exc
    schema_value = original_data.get("schema_version")
    if type(schema_value) is not int:
        raise ProjectValidationError("The project schema version must be an integer.")
    if schema_value == SCHEMA_VERSION:
        Project.from_dict(original_data)
        return ProjectMigrationResult(
            status="current",
            project=path.name,
            original_schema=SCHEMA_VERSION,
            target_schema=SCHEMA_VERSION,
            original_sha256=original_sha256,
            migrated_sha256=original_sha256,
            backup=None,
            receipt=None,
        )

    migrated_project, steps = migrate_project_data(original_data)
    migrated_raw = _project_payload_bytes(migrated_project)
    migrated_sha256 = _sha256(migrated_raw)
    artifacts = migration_artifact_paths(path, original_sha256, schema_value)
    plan = _build_plan(
        path=path,
        original_data=original_data,
        original_schema=schema_value,
        original_sha256=original_sha256,
        migrated_project=migrated_project,
        migrated_sha256=migrated_sha256,
        artifacts=artifacts,
        steps=steps,
    )
    pending = _pending_payload(plan)
    if _lexists(artifacts.pending):
        raise ProjectValidationError(
            "A pending migration appeared during preparation; retry safely."
        )
    if _lexists(artifacts.receipt):
        raise ProjectValidationError(
            "Migration receipt collision; nothing was changed: "
            f"{artifacts.receipt.name}"
        )

    resumed_prejournal = _lexists(artifacts.candidate) or _lexists(artifacts.backup)
    if _lexists(artifacts.candidate):
        existing_candidate, _ = snapshot_reader(artifacts.candidate)
        if _sha256(existing_candidate) != migrated_sha256:
            raise ProjectValidationError(
                "Existing pre-journal migration candidate does not match the "
                "reconstructed plan; it was left untouched for inspection."
            )
        Project.from_dict(decode_project_json(existing_candidate))
    if _lexists(artifacts.backup):
        existing_backup, _ = snapshot_reader(artifacts.backup)
        if _sha256(existing_backup) != original_sha256:
            raise ProjectValidationError(
                "Existing pre-journal migration backup does not match the "
                "original; it was left untouched for inspection."
            )
    if prepare_only:
        return None
    if write_lease is None:
        raise ProjectValidationError("Project migration write lease is missing.")

    created: list[tuple[Path, str, int]] = []
    try:
        if not _lexists(artifacts.candidate):
            write_lease.assert_current()
            _write_exclusive(artifacts.candidate, migrated_raw)
            created.append(
                (artifacts.candidate, migrated_sha256, MAX_PROJECT_FILE_BYTES)
            )
        if not _lexists(artifacts.backup):
            write_lease.assert_current()
            _write_exclusive(artifacts.backup, original_raw)
            created.append(
                (artifacts.backup, original_sha256, MAX_PROJECT_FILE_BYTES)
            )
        pending_raw = _pretty_json_bytes(pending)
        write_lease.assert_current()
        _write_exclusive(artifacts.pending, pending_raw)
        created.append(
            (artifacts.pending, _sha256(pending_raw), MAX_MIGRATION_AUX_BYTES)
        )
    except Exception:
        for artifact, digest, maximum in reversed(created):
            try:
                _unlink_verified(artifact, digest, maximum)
            except Exception:
                pass
        raise

    return _resume_pending(
        path,
        artifacts.pending,
        newly_prepared=not resumed_prejournal,
        write_lease=write_lease,
    )


def migrate_project_file(path: Path) -> ProjectMigrationResult:
    """Safely migrate one legacy project or finish its pending transaction."""

    path = canonical_target_path(path)
    preflight = _migrate_project_file_transaction(
        path, None, prepare_only=True
    )
    if preflight is not None:
        return preflight
    with exclusive_target_write_lease(path) as write_lease:
        write_lease.assert_current()
        result = _migrate_project_file_transaction(
            path, write_lease, prepare_only=False
        )
        if result is None:
            raise ProjectValidationError(
                "Project migration transaction ended without a result."
            )
        return result
