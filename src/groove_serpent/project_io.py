from __future__ import annotations

import json
import math
import os
import stat
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from .atomic_create import rename_no_replace
from .errors import ProjectValidationError
from .migration_fence import assert_no_pending_migration
from .models import MAX_PROJECT_REVISION, Project, utc_now_iso
from .portable_names import portable_path_entry_exists
from .transaction_lock import canonical_target_path, exclusive_target_write_lease

MAX_PROJECT_FILE_BYTES = 64 * 1024 * 1024


class _AutomaticExpectedProjectState:
    pass


_AUTOMATIC_EXPECTED_PROJECT_STATE = _AutomaticExpectedProjectState()


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int

    @classmethod
    def capture(cls, value: os.stat_result) -> "_FileIdentity":
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


def _plain_file_identity(path: Path) -> _FileIdentity:
    value = path.lstat()
    if (
        path.is_symlink()
        or _is_reparse(value)
        or not stat.S_ISREG(value.st_mode)
        or int(value.st_nlink) != 1
    ):
        raise ProjectValidationError(
            "Project path must be a single-link regular, non-reparse file: "
            f"{path.name}"
        )
    return _FileIdentity.capture(value)


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Invalid JSON number: {value}")
    return parsed


def _reject_duplicate_object_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def read_bounded_project_bytes(path: Path) -> bytes:
    """Read one bounded stable regular file through one descriptor."""

    before = _plain_file_identity(path)
    with path.open("rb") as handle:
        opened = _FileIdentity.capture(os.fstat(handle.fileno()))
        raw = handle.read(MAX_PROJECT_FILE_BYTES + 1)
    after = _plain_file_identity(path)
    if before != opened or opened != after:
        raise ProjectValidationError(
            "Project file identity changed while it was being read."
        )
    if len(raw) > MAX_PROJECT_FILE_BYTES:
        raise ProjectValidationError(
            f"Project file exceeds the {MAX_PROJECT_FILE_BYTES}-byte limit."
        )
    return raw


def decode_project_json(raw: bytes) -> dict[str, Any]:
    """Decode strict finite JSON and reject duplicate keys at every depth."""

    try:
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
        )
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds the supported depth.") from exc
    if not isinstance(data, dict):
        raise ValueError("The project root must be a JSON object.")
    return data


def save_project(
    project: Project,
    path: Path,
    *,
    expected_existing_sha256: (
        str | None | _AutomaticExpectedProjectState
    ) = _AUTOMATIC_EXPECTED_PROJECT_STATE,
) -> None:
    """Save with an OS lease and optional caller-boundary compare-and-swap.

    ``None`` means the caller observed an absent destination.  A digest means
    the caller observed exactly those bytes.  Omitting the argument retains
    the legacy function-entry snapshot for internal/test compatibility; every
    user-facing long-running mutation should pass an explicit expectation.
    """

    path = _absolute_without_resolving(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path = canonical_target_path(path)
    project.validate()
    automatic_expectation = (
        expected_existing_sha256 is _AUTOMATIC_EXPECTED_PROJECT_STATE
    )
    if not automatic_expectation and expected_existing_sha256 is not None:
        if (
            not isinstance(expected_existing_sha256, str)
            or len(expected_existing_sha256) != 64
            or expected_existing_sha256.lower() != expected_existing_sha256
            or any(character not in "0123456789abcdef" for character in expected_existing_sha256)
        ):
            raise ProjectValidationError(
                "Expected project SHA-256 must be a lowercase 64-character digest."
            )
    existed_before_lease = os.path.lexists(path)
    portable_before_lease = portable_path_entry_exists(path)
    identity_before_lease: _FileIdentity | None = None
    sha256_before_lease: str | None = None
    if existed_before_lease:
        identity_before_lease = _plain_file_identity(path)
        raw_before_lease = read_bounded_project_bytes(path)
        if _plain_file_identity(path) != identity_before_lease:
            raise ProjectValidationError(
                "Project file identity changed before the write lease was acquired."
            )
        sha256_before_lease = sha256(raw_before_lease).hexdigest()
    if not automatic_expectation:
        expected_exists = expected_existing_sha256 is not None
        if existed_before_lease != expected_exists:
            raise ProjectValidationError(
                "Project path no longer matches the caller's expected existence state."
            )
        if (
            expected_exists
            and sha256_before_lease != expected_existing_sha256
        ):
            raise ProjectValidationError(
                "Project file changed after the caller loaded it; reload before saving."
            )
        if not expected_exists and portable_before_lease:
            raise ProjectValidationError(
                "An NFC/case-equivalent project destination already exists."
            )
    with exclusive_target_write_lease(path) as write_lease:
        write_lease.assert_current()
        assert_no_pending_migration(path, "project")
        existed = os.path.lexists(path)
        portable_exists = portable_path_entry_exists(path)
        if (
            existed != existed_before_lease
            or portable_exists != portable_before_lease
        ):
            raise ProjectValidationError(
                "Project path existence changed while waiting for the write lease."
            )
        original_identity = _plain_file_identity(path) if existed else None
        if not existed and portable_exists:
            raise ProjectValidationError(
                "An NFC/case-equivalent project destination already exists."
            )
        if existed:
            existing_raw = read_bounded_project_bytes(path)
            if _plain_file_identity(path) != original_identity:
                raise ProjectValidationError(
                    "Project file identity changed before revision validation."
                )
            if (
                original_identity != identity_before_lease
                or sha256(existing_raw).hexdigest() != sha256_before_lease
            ):
                raise ProjectValidationError(
                    "Project file changed while waiting for the write lease."
                )
            try:
                existing = Project.from_dict(decode_project_json(existing_raw))
            except (KeyError, TypeError, ValueError) as exc:
                raise ProjectValidationError(
                    "Existing project is invalid and cannot be safely replaced."
                ) from exc
            if existing.revision != project.revision:
                raise ProjectValidationError(
                    "Project revision changed; reload before saving."
                )
            if existing.revision >= MAX_PROJECT_REVISION:
                raise ProjectValidationError(
                    "Project revision is exhausted and cannot be incremented."
                )
        next_revision = project.revision + 1 if existed else project.revision
        next_updated_at = utc_now_iso()
        serialized = project.to_dict()
        serialized["revision"] = next_revision
        serialized["updated_at"] = next_updated_at
        payload = (
            json.dumps(serialized, indent=2, ensure_ascii=False, allow_nan=False)
            + "\n"
        )
        if len(payload.encode("utf-8")) > MAX_PROJECT_FILE_BYTES:
            raise ProjectValidationError(
                f"Project file exceeds the {MAX_PROJECT_FILE_BYTES}-byte limit."
            )
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(
                descriptor, "w", encoding="utf-8", newline="\n"
            ) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if original_identity is None:
                if os.path.lexists(path) or portable_path_entry_exists(path):
                    raise ProjectValidationError(
                        "Project path appeared before atomic save; refusing to replace it."
                    )
            elif _plain_file_identity(path) != original_identity:
                raise ProjectValidationError(
                    "Project file identity changed before atomic save."
                )
            write_lease.assert_current()
            if original_identity is None:
                try:
                    rename_no_replace(temporary, path)
                except FileExistsError as exc:
                    raise ProjectValidationError(
                        "Project path appeared before atomic creation."
                    ) from exc
            else:
                os.replace(temporary, path)
            project.revision = next_revision
            project.updated_at = next_updated_at
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def load_project_with_sha256(path: Path) -> tuple[Project, str]:
    path = _absolute_without_resolving(path)
    try:
        raw = read_bounded_project_bytes(path)
        data = decode_project_json(raw)
        return Project.from_dict(data), sha256(raw).hexdigest()
    except ProjectValidationError:
        raise
    except (
        AttributeError,
        KeyError,
        OverflowError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise ProjectValidationError(f"Project file is invalid: {exc}") from exc


def load_project(path: Path) -> Project:
    return load_project_with_sha256(path)[0]
