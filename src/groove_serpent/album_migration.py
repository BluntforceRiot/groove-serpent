from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from .atomic_create import rename_no_replace
from .album import (
    ALBUM_SCHEMA,
    ALBUM_SCHEMA_V2,
    LEGACY_ALBUM_SCHEMA,
    MAX_ALBUM_FILE_BYTES,
    AlbumArtwork,
    AlbumProject,
    AlbumSide,
    _is_reparse,
    _read_stable_album_bytes,
    _validate_album_timestamp,
    canonical_album_path,
    resolve_album_reference,
)
from .errors import ProjectValidationError
from .migration_commit import (
    prepare_replacement,
    quarantine_path_no_replace,
    remove_exact_plain_file,
)
from .models import SCHEMA_VERSION, utc_now_iso
from .project_io import decode_project_json, load_project_with_sha256
from .transaction_lock import TargetWriteLease, exclusive_target_write_lease

ALBUM_MIGRATION_PLAN_SCHEMA = "groove-serpent.album-migration-plan/1"
ALBUM_MIGRATION_PENDING_SCHEMA = "groove-serpent.album-migration-pending/1"
ALBUM_MIGRATION_RECEIPT_SCHEMA = "groove-serpent.album-migration-receipt/1"
MAX_ALBUM_MIGRATION_AUX_BYTES = 1024 * 1024

# Keep the read-only preflight distinct from the authoritative under-lease
# transaction so race instrumentation can target either phase precisely.
_read_preflight_album_bytes = _read_stable_album_bytes

_ROOT_V1_V2_FIELDS = {
    "schema",
    "created_at",
    "updated_at",
    "metadata",
    "artwork",
    "sides",
}
_SIDE_V1_FIELDS = {
    "label",
    "order",
    "project",
    "capture_rpm",
    "intended_rpm",
    "fine_factor",
}


@dataclass(frozen=True, slots=True)
class AlbumMigrationArtifactPaths:
    backup: Path
    candidate: Path
    pending: Path
    receipt: Path


@dataclass(frozen=True, slots=True)
class AlbumMigrationResult:
    status: Literal["current", "migrated", "recovered"]
    album: str
    original_schema: str
    target_schema: str
    original_sha256: str
    migrated_sha256: str
    backup: str | None
    receipt: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "groove-serpent.album-migration-result/1",
            "status": self.status,
            "album": self.album,
            "original_schema": self.original_schema,
            "target_schema": self.target_schema,
            "original_sha256": self.original_sha256,
            "migrated_sha256": self.migrated_sha256,
            "backup": self.backup,
            "receipt": self.receipt,
        }


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProjectValidationError(
            f"Album migration data is not canonical finite JSON: {exc}"
        ) from exc


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
            f"Album migration data is not finite JSON: {exc}"
        ) from exc


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in value
    ):
        raise ProjectValidationError(f"{label} must be a JSON object with text keys.")
    return value


def _require_exact_keys(
    value: Any, expected: set[str], label: str
) -> dict[str, Any]:
    data = _require_mapping(value, label)
    missing = expected - data.keys()
    unexpected = data.keys() - expected
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


def _validate_common_legacy_root(data: dict[str, Any], schema: str) -> None:
    _require_exact_keys(data, _ROOT_V1_V2_FIELDS, f"Album {schema}")
    if data["schema"] != schema:
        raise ProjectValidationError(f"Album payload does not match schema {schema!r}.")
    _validate_album_timestamp(data["created_at"], "Album created_at")
    _validate_album_timestamp(data["updated_at"], "Album updated_at")
    if not isinstance(data["metadata"], dict):
        raise ProjectValidationError("Album metadata must be a JSON object.")
    if data["artwork"] is not None:
        AlbumArtwork.from_dict(data["artwork"])
    sides = _require_list(data["sides"], "Album sides")
    if not sides:
        raise ProjectValidationError("An album project must contain at least one side.")


def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    _validate_common_legacy_root(data, LEGACY_ALBUM_SCHEMA)
    migrated = copy.deepcopy(data)
    migrated_sides: list[dict[str, Any]] = []
    for index, value in enumerate(
        _require_list(data["sides"], "Album sides"), start=1
    ):
        _require_exact_keys(value, _SIDE_V1_FIELDS, f"Legacy album side {index}")
        side = AlbumSide.from_legacy_dict(value)
        if side.pin is not None:
            raise ProjectValidationError("Legacy album migration must not create pins.")
        migrated_sides.append(asdict(side))
    migrated["sides"] = migrated_sides
    migrated["schema"] = ALBUM_SCHEMA_V2
    return migrated


def _migrate_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    _validate_common_legacy_root(data, ALBUM_SCHEMA_V2)
    raw_sides = _require_list(data["sides"], "Album sides")
    for value in raw_sides:
        AlbumSide.from_dict(value)
    migrated = copy.deepcopy(data)
    migrated["schema"] = ALBUM_SCHEMA
    migrated["revision"] = 1
    project = AlbumProject.from_dict(migrated)
    return project.to_dict()


_MIGRATORS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    LEGACY_ALBUM_SCHEMA: _migrate_v1_to_v2,
    ALBUM_SCHEMA_V2: _migrate_v2_to_v3,
}


def _migration_steps(original_schema: str) -> list[dict[str, str]]:
    v2_to_v3 = {
        "from": ALBUM_SCHEMA_V2,
        "to": ALBUM_SCHEMA,
        "effect": "added revision 1; metadata, artwork, sides, speed, and pins unchanged",
    }
    if original_schema == LEGACY_ALBUM_SCHEMA:
        return [
            {
                "from": LEGACY_ALBUM_SCHEMA,
                "to": ALBUM_SCHEMA_V2,
                "effect": (
                    "retained each legacy speed as an explicit override; pin remains null"
                ),
            },
            v2_to_v3,
        ]
    if original_schema == ALBUM_SCHEMA_V2:
        return [v2_to_v3]
    return []


def migrate_album_data(
    data: dict[str, Any]
) -> tuple[AlbumProject, tuple[dict[str, str], ...]]:
    """Apply the exact sequential album registry and validate schema /3."""

    working = copy.deepcopy(_require_mapping(data, "Album project"))
    schema = working.get("schema")
    if schema == ALBUM_SCHEMA:
        return AlbumProject.from_dict(working), ()
    if schema not in _MIGRATORS:
        if isinstance(schema, str) and schema.startswith("groove-serpent.album/"):
            raise ProjectValidationError(
                f"Album schema {schema!r} cannot be migrated to {ALBUM_SCHEMA!r}."
            )
        raise ProjectValidationError(f"Unsupported album schema {schema!r}.")
    original_schema = schema
    steps: list[dict[str, str]] = []
    while schema != ALBUM_SCHEMA:
        migrator = _MIGRATORS.get(schema)
        if migrator is None:
            raise ProjectValidationError(
                f"No sequential album migration is registered from {schema!r}."
            )
        working = migrator(working)
        new_schema = working.get("schema")
        expected_steps = _migration_steps(original_schema)
        step = expected_steps[len(steps)]
        if new_schema != step["to"] or schema != step["from"]:
            raise ProjectValidationError("Album migration registry is not sequential.")
        steps.append(step)
        if not isinstance(new_schema, str):
            raise ProjectValidationError("Album migration produced an invalid schema.")
        schema = new_schema
    return AlbumProject.from_dict(working), tuple(steps)


def _schema_number(schema: str) -> int:
    if schema == LEGACY_ALBUM_SCHEMA:
        return 1
    if schema == ALBUM_SCHEMA_V2:
        return 2
    if schema == ALBUM_SCHEMA:
        return 3
    raise ProjectValidationError(f"Unsupported album schema {schema!r}.")


def album_migration_artifact_paths(
    album_path: Path, original_sha256: str, original_schema: str
) -> AlbumMigrationArtifactPaths:
    album_path = canonical_album_path(album_path)
    filename_id = _sha256(album_path.name.encode("utf-8"))[:12]
    base = (
        f".groove-serpent-album-migration-{filename_id}-"
        f"{original_sha256[:16]}-v{_schema_number(original_schema)}-v3"
    )
    return AlbumMigrationArtifactPaths(
        backup=album_path.parent / f"{base}.backup",
        candidate=album_path.parent / f"{base}.candidate",
        pending=album_path.parent / f"{base}.pending.json",
        receipt=album_path.parent / f"{base}.receipt.json",
    )


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _assert_plain_parent(path: Path) -> tuple[int, int, int, int]:
    value = path.parent.lstat()
    if (
        path.parent.is_symlink()
        or _is_reparse(value)
        or not stat.S_ISDIR(value.st_mode)
    ):
        raise ProjectValidationError(
            "Album migration requires a regular, non-reparse parent directory."
        )
    return (value.st_dev, value.st_ino, value.st_mode, value.st_mtime_ns)


def _write_exclusive(path: Path, payload: bytes) -> None:
    if _lexists(path):
        raise ProjectValidationError(
            f"Album migration artifact already exists: {path.name}"
        )
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ProjectValidationError(
            f"Album migration artifact already exists: {path.name}"
        ) from exc


def _read_artifact(path: Path, maximum: int) -> bytes:
    raw, _ = _read_stable_album_bytes(path, maximum=maximum)
    return raw


def _read_preflight_artifact(path: Path, maximum: int) -> bytes:
    raw, _ = _read_preflight_album_bytes(path, maximum=maximum)
    return raw


def _replace_sibling(source: Path, destination: Path) -> None:
    """Replace within one verified parent, using a directory fd on POSIX."""

    if source.parent != destination.parent:
        raise ProjectValidationError(
            "Album migration replacement source and target must share one parent."
        )
    if os.name != "nt" and os.rename in os.supports_dir_fd:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(destination.parent, flags)
        try:
            value = os.fstat(descriptor)
            opened = (value.st_dev, value.st_ino, value.st_mode, value.st_mtime_ns)
            if opened != _assert_plain_parent(destination):
                raise ProjectValidationError(
                    "Album migration parent identity changed before replacement."
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


def _restore_album_backup(
    path: Path,
    backup_raw: bytes,
    original_sha256: str,
    write_lease: TargetWriteLease,
) -> None:
    """Restore exact bytes without overwriting an unowned path replacement."""

    try:
        current_raw, _ = _read_stable_album_bytes(path)
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
                        path, purpose="album-migration-rollback-conflict"
                    )
                )
            except BaseException as exc:
                last_error = exc
                continue
        prepared = prepare_replacement(
            path,
            backup_raw,
            maximum=MAX_ALBUM_FILE_BYTES,
            purpose="album-migration-rollback",
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
                restored_raw, _ = _read_stable_album_bytes(path)
                if (
                    restored_raw == backup_raw
                    and _sha256(restored_raw) == original_sha256
                ):
                    return
            last_error = ProjectValidationError(
                "Album migration rollback did not install the verified backup."
            )
        finally:
            prepared.discard()
    raise ProjectValidationError(
        "Album migration detected an unsafe post-commit mismatch and could not "
        "restore the verified backup without overwriting a conflict. "
        f"Preserved: {', '.join(item.name for item in preserved) or 'none'}. "
        f"Last error: {last_error}"
    ) from last_error


def _commit_album_candidate(
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
        maximum=MAX_ALBUM_FILE_BYTES,
        purpose="album-migration-commit",
    )
    try:
        write_lease.assert_current()
        try:
            _replace_sibling(prepared.path, path)
        except BaseException:
            if not prepared.matches_target(path):
                _restore_album_backup(
                    path, backup_raw, original_sha256, write_lease
                )
            raise
        if not prepared.matches_target(path):
            _restore_album_backup(path, backup_raw, original_sha256, write_lease)
            raise ProjectValidationError(
                "Album migration replacement did not install the descriptor-bound "
                "candidate; the verified backup was restored."
            )
        try:
            write_lease.assert_current()
            _unlink_verified(candidate, migrated_sha256, MAX_ALBUM_FILE_BYTES)
        except BaseException as exc:
            _restore_album_backup(path, backup_raw, original_sha256, write_lease)
            raise ProjectValidationError(
                "Album migration candidate identity changed at commit; the "
                "verified backup was restored."
            ) from exc
        if _lexists(candidate):
            _restore_album_backup(path, backup_raw, original_sha256, write_lease)
            raise ProjectValidationError(
                "Album migration candidate reappeared during commit; the "
                "verified backup was restored."
            )
    finally:
        prepared.discard()


def _unlink_verified(path: Path, expected_sha256: str, maximum: int) -> None:
    remove_exact_plain_file(
        path,
        expected_sha256,
        maximum=maximum,
        purpose="album-migration-artifact-cleanup",
    )


def _candidate_bytes(album: AlbumProject) -> bytes:
    payload = _pretty_json_bytes(album.to_dict())
    if len(payload) > MAX_ALBUM_FILE_BYTES:
        raise ProjectValidationError(
            f"Migrated album exceeds the {MAX_ALBUM_FILE_BYTES}-byte limit."
        )
    reproduced = AlbumProject.from_dict(decode_project_json(payload))
    if reproduced.to_dict() != album.to_dict():
        raise ProjectValidationError(
            "Migrated album did not reproduce after canonical serialization."
        )
    return payload


def _side_project_identities(
    album: AlbumProject, album_path: Path
) -> list[dict[str, Any]]:
    identities: list[dict[str, Any]] = []
    for side in album.sides:
        project_path = resolve_album_reference(
            album_path, side.project, "Album side project reference"
        )
        try:
            project, project_sha256 = load_project_with_sha256(project_path)
        except (OSError, ProjectValidationError) as exc:
            raise ProjectValidationError(
                f"Side {side.label} project {side.project!r} must be migrated first "
                "with 'groove-serpent project migrate PROJECT'; no album migration "
                "commit can proceed."
            ) from exc
        if project.schema_version != SCHEMA_VERSION:
            raise ProjectValidationError(
                f"Side {side.label} project {side.project!r} is not current schema "
                f"{SCHEMA_VERSION}; migrate it before the album."
            )
        identities.append(
            {
                "label": side.label,
                "project": side.project,
                "project_sha256": project_sha256,
                "project_revision": project.revision,
                "editable_state_sha256": project.state_sha256,
                "source_sha256": project.source.sha256.lower(),
            }
        )
    return identities


_preflight_side_project_identities = _side_project_identities


def _build_plan(
    *,
    path: Path,
    original: dict[str, Any],
    original_schema: str,
    original_sha256: str,
    migrated: AlbumProject,
    migrated_sha256: str,
    artifacts: AlbumMigrationArtifactPaths,
    steps: tuple[dict[str, str], ...],
    side_projects: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": ALBUM_MIGRATION_PLAN_SCHEMA,
        "album": path.name,
        "album_filename_sha256": _sha256(path.name.encode("utf-8")),
        "original_schema": original_schema,
        "target_schema": ALBUM_SCHEMA,
        "steps": list(steps),
        "original_sha256": original_sha256,
        "migrated_sha256": migrated_sha256,
        "migrated_revision": migrated.revision,
        "album_state_sha256": _sha256(_canonical_json_bytes(migrated.to_dict())),
        "side_projects": side_projects,
        "backup": artifacts.backup.name,
        "candidate": artifacts.candidate.name,
        "receipt": artifacts.receipt.name,
        "original_timestamps": {
            "created_at": original["created_at"],
            "updated_at": original["updated_at"],
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


def _validate_side_project_plan(value: Any) -> list[dict[str, Any]]:
    items = _require_list(value, "Album migration side-project identities")
    validated: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        data = _require_exact_keys(
            item,
            {
                "label",
                "project",
                "project_sha256",
                "project_revision",
                "editable_state_sha256",
                "source_sha256",
            },
            f"Album migration side-project identity {index}",
        )
        if not isinstance(data["label"], str) or not isinstance(data["project"], str):
            raise ProjectValidationError("Album migration side identity text is invalid.")
        if type(data["project_revision"]) is not int or data["project_revision"] < 1:
            raise ProjectValidationError("Album migration side revision is invalid.")
        _validate_digest(data["project_sha256"], "Side project SHA-256")
        _validate_digest(
            data["editable_state_sha256"], "Side editable-state SHA-256"
        )
        source_sha256 = data["source_sha256"]
        if source_sha256 != "":
            _validate_digest(source_sha256, "Side source SHA-256")
        validated.append(data)
    return validated


def _validate_plan(
    value: Any, path: Path
) -> tuple[dict[str, Any], AlbumMigrationArtifactPaths]:
    plan = _require_exact_keys(
        value,
        {
            "schema",
            "album",
            "album_filename_sha256",
            "original_schema",
            "target_schema",
            "steps",
            "original_sha256",
            "migrated_sha256",
            "migrated_revision",
            "album_state_sha256",
            "side_projects",
            "backup",
            "candidate",
            "receipt",
            "original_timestamps",
        },
        "Album migration plan",
    )
    if plan["schema"] != ALBUM_MIGRATION_PLAN_SCHEMA or plan["album"] != path.name:
        raise ProjectValidationError("Album migration plan targets a different album.")
    if plan["album_filename_sha256"] != _sha256(path.name.encode("utf-8")):
        raise ProjectValidationError("Album migration filename identity does not match.")
    original_schema = plan["original_schema"]
    if original_schema not in {LEGACY_ALBUM_SCHEMA, ALBUM_SCHEMA_V2}:
        raise ProjectValidationError("Album migration original schema is invalid.")
    if plan["target_schema"] != ALBUM_SCHEMA:
        raise ProjectValidationError("Album migration target schema is invalid.")
    if plan["steps"] != _migration_steps(original_schema):
        raise ProjectValidationError("Album migration steps are not exact and sequential.")
    original_sha256 = _validate_digest(
        plan["original_sha256"], "Album migration original SHA-256"
    )
    _validate_digest(plan["migrated_sha256"], "Album migration result SHA-256")
    _validate_digest(plan["album_state_sha256"], "Album migration state SHA-256")
    if plan["migrated_revision"] != 1:
        raise ProjectValidationError("Migrated album revision must be 1.")
    _validate_side_project_plan(plan["side_projects"])
    timestamps = _require_exact_keys(
        plan["original_timestamps"],
        {"created_at", "updated_at"},
        "Album migration original timestamps",
    )
    _validate_album_timestamp(timestamps["created_at"], "Original created_at")
    _validate_album_timestamp(timestamps["updated_at"], "Original updated_at")
    artifacts = album_migration_artifact_paths(path, original_sha256, original_schema)
    for key, expected in (
        ("backup", artifacts.backup),
        ("candidate", artifacts.candidate),
        ("receipt", artifacts.receipt),
    ):
        name = plan[key]
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or name != expected.name
        ):
            raise ProjectValidationError(
                f"Album migration {key} is not the expected portable sibling."
            )
    return plan, artifacts


def _pending_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": ALBUM_MIGRATION_PENDING_SCHEMA,
        "plan": plan,
        "plan_sha256": _sha256(_canonical_json_bytes(plan)),
    }


def _read_aux(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = _read_artifact(path, MAX_ALBUM_MIGRATION_AUX_BYTES)
    try:
        return decode_project_json(raw), raw
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectValidationError(
            f"Album migration artifact {path.name} is invalid JSON: {exc}"
        ) from exc


def _read_pending(
    path: Path, pending_path: Path
) -> tuple[dict[str, Any], AlbumMigrationArtifactPaths, str]:
    payload, raw = _read_aux(pending_path)
    _require_exact_keys(
        payload,
        {"schema", "plan", "plan_sha256"},
        "Pending album migration journal",
    )
    if payload["schema"] != ALBUM_MIGRATION_PENDING_SCHEMA:
        raise ProjectValidationError("Pending album migration schema is invalid.")
    plan, artifacts = _validate_plan(payload["plan"], path)
    plan_sha256 = _validate_digest(
        payload["plan_sha256"], "Pending album migration plan SHA-256"
    )
    if plan_sha256 != _sha256(_canonical_json_bytes(plan)):
        raise ProjectValidationError("Pending album migration plan hash does not match.")
    if artifacts.pending != pending_path:
        raise ProjectValidationError("Pending album migration journal name is invalid.")
    return plan, artifacts, _sha256(raw)


def _find_pending(path: Path) -> Path | None:
    filename_id = _sha256(path.name.encode("utf-8"))[:12]
    match: Path | None = None
    for candidate in path.parent.glob(
        f".groove-serpent-album-migration-{filename_id}-*.pending.json"
    ):
        if match is not None:
            raise ProjectValidationError(
                "Multiple pending album migrations target this album."
            )
        match = candidate
    return match


def _validate_receipt(
    receipt_path: Path, plan: dict[str, Any], plan_sha256: str
) -> None:
    receipt, _ = _read_aux(receipt_path)
    _require_exact_keys(
        receipt,
        {"schema", "status", "plan", "plan_sha256", "committed_at"},
        "Album migration receipt",
    )
    if (
        receipt["schema"] != ALBUM_MIGRATION_RECEIPT_SCHEMA
        or receipt["status"] != "committed"
        or receipt["plan"] != plan
        or receipt["plan_sha256"] != plan_sha256
    ):
        raise ProjectValidationError("Existing album migration receipt is inconsistent.")
    _validate_album_timestamp(receipt["committed_at"], "Receipt committed_at")


def _result(
    plan: dict[str, Any], status: Literal["migrated", "recovered"]
) -> AlbumMigrationResult:
    return AlbumMigrationResult(
        status=status,
        album=str(plan["album"]),
        original_schema=str(plan["original_schema"]),
        target_schema=ALBUM_SCHEMA,
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
) -> AlbumMigrationResult:
    plan, artifacts, pending_sha256 = _read_pending(path, pending_path)
    original_sha256 = str(plan["original_sha256"])
    migrated_sha256 = str(plan["migrated_sha256"])
    backup_raw = _read_artifact(artifacts.backup, MAX_ALBUM_FILE_BYTES)
    if _sha256(backup_raw) != original_sha256:
        raise ProjectValidationError("Album migration backup hash does not match.")

    parent_identity = _assert_plain_parent(path)
    current_raw, current_identity = _read_stable_album_bytes(path)
    current_sha256 = _sha256(current_raw)
    if current_sha256 == original_sha256:
        if _lexists(artifacts.receipt):
            raise ProjectValidationError(
                "A receipt exists while the legacy album is still present."
            )
        candidate_raw = _read_artifact(artifacts.candidate, MAX_ALBUM_FILE_BYTES)
        if _sha256(candidate_raw) != migrated_sha256:
            raise ProjectValidationError("Album migration candidate hash does not match.")
        candidate_album = AlbumProject.from_dict(decode_project_json(candidate_raw))
        if _sha256(_canonical_json_bytes(candidate_album.to_dict())) != plan[
            "album_state_sha256"
        ]:
            raise ProjectValidationError("Album migration candidate state does not match.")
        if _side_project_identities(candidate_album, path) != plan["side_projects"]:
            raise ProjectValidationError(
                "A referenced side project changed during album migration."
            )
        repeated_raw, repeated_identity = _read_stable_album_bytes(path)
        if (
            repeated_identity != current_identity
            or _sha256(repeated_raw) != original_sha256
            or _assert_plain_parent(path) != parent_identity
        ):
            raise ProjectValidationError(
                "Album or parent identity changed before atomic replacement."
            )
        _commit_album_candidate(
            path,
            artifacts.candidate,
            candidate_raw,
            migrated_sha256,
            backup_raw,
            original_sha256,
            write_lease,
        )
        current_raw, _ = _read_stable_album_bytes(path)
        current_sha256 = _sha256(current_raw)

    if current_sha256 != migrated_sha256:
        raise ProjectValidationError(
            "Pending album migration conflicts with current album bytes."
        )
    if _lexists(artifacts.candidate):
        candidate_raw = _read_artifact(
            artifacts.candidate, MAX_ALBUM_FILE_BYTES
        )
        if _sha256(candidate_raw) != migrated_sha256:
            raise ProjectValidationError(
                "The migrated album and a changed candidate both exist; state "
                "is ambiguous."
            )
        write_lease.assert_current()
        _unlink_verified(
            artifacts.candidate, migrated_sha256, MAX_ALBUM_FILE_BYTES
        )
    migrated_album = AlbumProject.from_dict(decode_project_json(current_raw))
    if _sha256(_canonical_json_bytes(migrated_album.to_dict())) != plan[
        "album_state_sha256"
    ]:
        raise ProjectValidationError("Migrated album state does not match the plan.")
    if _side_project_identities(migrated_album, path) != plan["side_projects"]:
        raise ProjectValidationError(
            "A referenced side project changed before migration commit."
        )

    plan_sha256 = _sha256(_canonical_json_bytes(plan))
    if _lexists(artifacts.receipt):
        _validate_receipt(artifacts.receipt, plan, plan_sha256)
    else:
        receipt = {
            "schema": ALBUM_MIGRATION_RECEIPT_SCHEMA,
            "status": "committed",
            "plan": plan,
            "plan_sha256": plan_sha256,
            "committed_at": utc_now_iso(),
        }
        write_lease.assert_current()
        _write_exclusive(artifacts.receipt, _pretty_json_bytes(receipt))
    write_lease.assert_current()
    _unlink_verified(
        pending_path, pending_sha256, MAX_ALBUM_MIGRATION_AUX_BYTES
    )
    status: Literal["migrated", "recovered"] = (
        "migrated" if newly_prepared else "recovered"
    )
    return _result(plan, status)


def _migrate_album_file_transaction(
    path: Path,
    write_lease: TargetWriteLease | None,
    *,
    prepare_only: bool,
) -> AlbumMigrationResult | None:
    """Validate once without writes, or run with the target lease held."""

    _assert_plain_parent(path)
    pending_path = _find_pending(path)
    if pending_path is not None:
        if prepare_only:
            return None
        if write_lease is None:
            raise ProjectValidationError("Album migration write lease is missing.")
        return _resume_pending(
            path,
            pending_path,
            newly_prepared=False,
            write_lease=write_lease,
        )

    album_reader = (
        _read_preflight_album_bytes if prepare_only else _read_stable_album_bytes
    )
    artifact_reader = _read_preflight_artifact if prepare_only else _read_artifact
    side_identity_reader = (
        _preflight_side_project_identities
        if prepare_only
        else _side_project_identities
    )
    original_raw, _ = album_reader(path)
    original_sha256 = _sha256(original_raw)
    try:
        original = decode_project_json(original_raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectValidationError(f"Album project is invalid: {exc}") from exc
    schema = original.get("schema")
    if schema == ALBUM_SCHEMA:
        AlbumProject.from_dict(original)
        return AlbumMigrationResult(
            status="current",
            album=path.name,
            original_schema=ALBUM_SCHEMA,
            target_schema=ALBUM_SCHEMA,
            original_sha256=original_sha256,
            migrated_sha256=original_sha256,
            backup=None,
            receipt=None,
        )
    if not isinstance(schema, str):
        raise ProjectValidationError("Album schema must be text.")
    migrated, steps = migrate_album_data(original)

    # This complete schema/currentness preflight precedes every transaction write.
    side_projects = side_identity_reader(migrated, path)
    migrated_raw = _candidate_bytes(migrated)
    migrated_sha256 = _sha256(migrated_raw)
    artifacts = album_migration_artifact_paths(path, original_sha256, schema)
    plan = _build_plan(
        path=path,
        original=original,
        original_schema=schema,
        original_sha256=original_sha256,
        migrated=migrated,
        migrated_sha256=migrated_sha256,
        artifacts=artifacts,
        steps=steps,
        side_projects=side_projects,
    )
    if _lexists(artifacts.pending):
        raise ProjectValidationError("A pending album migration appeared; retry.")
    if _lexists(artifacts.receipt):
        raise ProjectValidationError(
            f"Album migration receipt collision: {artifacts.receipt.name}"
        )

    resumed_prejournal = _lexists(artifacts.candidate) or _lexists(artifacts.backup)
    if _lexists(artifacts.candidate):
        existing = artifact_reader(artifacts.candidate, MAX_ALBUM_FILE_BYTES)
        if _sha256(existing) != migrated_sha256:
            raise ProjectValidationError(
                "Existing album migration candidate is inconsistent and was "
                "left untouched."
            )
        AlbumProject.from_dict(decode_project_json(existing))
    if _lexists(artifacts.backup):
        existing = artifact_reader(artifacts.backup, MAX_ALBUM_FILE_BYTES)
        if _sha256(existing) != original_sha256:
            raise ProjectValidationError(
                "Existing album migration backup is inconsistent and was left "
                "untouched."
            )
    if prepare_only:
        return None
    if write_lease is None:
        raise ProjectValidationError("Album migration write lease is missing.")

    created: list[tuple[Path, str, int]] = []
    try:
        if not _lexists(artifacts.candidate):
            write_lease.assert_current()
            _write_exclusive(artifacts.candidate, migrated_raw)
            created.append(
                (artifacts.candidate, migrated_sha256, MAX_ALBUM_FILE_BYTES)
            )
        if not _lexists(artifacts.backup):
            write_lease.assert_current()
            _write_exclusive(artifacts.backup, original_raw)
            created.append((artifacts.backup, original_sha256, MAX_ALBUM_FILE_BYTES))
        pending_raw = _pretty_json_bytes(_pending_payload(plan))
        write_lease.assert_current()
        _write_exclusive(artifacts.pending, pending_raw)
        created.append(
            (
                artifacts.pending,
                _sha256(pending_raw),
                MAX_ALBUM_MIGRATION_AUX_BYTES,
            )
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


def migrate_album_file(path: Path) -> AlbumMigrationResult:
    """Migrate one album transactionally or finish its pending transaction."""

    path = canonical_album_path(path)
    preflight = _migrate_album_file_transaction(
        path, None, prepare_only=True
    )
    if preflight is not None:
        return preflight
    with exclusive_target_write_lease(path) as write_lease:
        write_lease.assert_current()
        result = _migrate_album_file_transaction(
            path, write_lease, prepare_only=False
        )
        if result is None:
            raise ProjectValidationError(
                "Album migration transaction ended without a result."
            )
        return result
