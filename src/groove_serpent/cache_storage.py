"""Crash-safe snapshot leases and deterministic storage preflights.

Snapshot directories can be as large as the physical capture they protect.  A
normal context-manager exit removes them, but a hard process termination cannot
run Python cleanup.  This module records enough ownership evidence to reclaim
only Groove Serpent directories whose owner is provably gone.  Ambiguous and
live owners are always left alone.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from . import __version__
from .errors import GrooveSerpentError


SNAPSHOT_LEASE_SCHEMA = "groove-serpent.snapshot-lease/1"
SNAPSHOT_DIRECTORY_PREFIX = "groove-serpent-audio-"
SNAPSHOT_LEASE_FILENAME = "snapshot-lease.json"
CACHE_ENVIRONMENT_VARIABLE = "GROOVE_SERPENT_CACHE_DIR"
DEFAULT_STORAGE_RESERVE_BYTES = 64 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")

OwnerStatus = Literal["live", "dead", "reused", "unknown"]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _strict_nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GrooveSerpentError(f"{label} must be a non-negative integer.")
    return value


def _process_creation_identity_linux(pid: int) -> str | None:
    """Return Linux kernel start ticks, which distinguish a reused PID."""

    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        raw = stat_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    closing = raw.rfind(")")
    if closing < 0:
        return None
    fields = raw[closing + 1 :].split()
    # The tail begins with field 3 (state); process start time is field 22.
    if len(fields) <= 19 or not fields[19].isdigit():
        return None
    return f"linux-start-ticks:{fields[19]}"


class _WindowsFileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


def _process_creation_identity_windows(pid: int) -> str | None:
    """Return Windows process creation FILETIME when the API is available."""

    loader: Any = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return None
    try:
        kernel32: Any = loader("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        filetime_pointer = ctypes.POINTER(_WindowsFileTime)
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            filetime_pointer,
            filetime_pointer,
            filetime_pointer,
            filetime_pointer,
        ]
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        creation = _WindowsFileTime()
        exit_time = _WindowsFileTime()
        kernel_time = _WindowsFileTime()
        user_time = _WindowsFileTime()
        try:
            succeeded = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            )
        finally:
            kernel32.CloseHandle(handle)
        if not succeeded:
            return None
        value = (int(creation.high) << 32) | int(creation.low)
        return f"windows-creation-filetime:{value}"
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def process_creation_identity(pid: int | None = None) -> str | None:
    """Return a PID-reuse-resistant identity on supported local platforms."""

    selected = os.getpid() if pid is None else pid
    if isinstance(selected, bool) or not isinstance(selected, int) or selected <= 0:
        return None
    if os.name == "nt":
        return _process_creation_identity_windows(selected)
    if Path("/proc").is_dir():
        return _process_creation_identity_linux(selected)
    return None


def _pid_exists_windows(pid: int) -> bool | None:
    loader: Any = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return None
    try:
        kernel32: Any = loader("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        # SYNCHRONIZE plus limited query lets a zero-time wait distinguish an
        # exited process object that Windows still exposes by PID.
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)
        if handle:
            try:
                wait_result = int(kernel32.WaitForSingleObject(handle, 0))
            finally:
                kernel32.CloseHandle(handle)
            if wait_result == 0:  # WAIT_OBJECT_0
                return False
            if wait_result == 258:  # WAIT_TIMEOUT
                return True
            return None
        get_last_error: Any = getattr(ctypes, "get_last_error", None)
        error = int(get_last_error()) if get_last_error is not None else 0
        # ERROR_INVALID_PARAMETER means there is currently no process with PID.
        if error == 87:
            return False
        # Access denied may describe a protected but live system process.
        if error == 5:
            return None
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return None


def _pid_exists(pid: int) -> bool | None:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _pid_exists_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno in {errno.EPERM, errno.EACCES}:
            return None
        return None
    return True


@dataclass(frozen=True, slots=True)
class SnapshotLeaseMetadata:
    """On-disk proof that one process owns a snapshot directory."""

    schema: str
    source_sha256: str | None
    source_size_bytes: int
    owner_pid: int
    owner_process_creation_identity: str | None
    created_at: str
    app_version: str
    lease_state: Literal["capturing", "active", "released"]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> "SnapshotLeaseMetadata":
        if not isinstance(value, dict):
            raise ValueError("Snapshot lease must be a JSON object.")
        expected = {
            "schema",
            "source_sha256",
            "source_size_bytes",
            "owner_pid",
            "owner_process_creation_identity",
            "created_at",
            "app_version",
            "lease_state",
        }
        if set(value) != expected:
            raise ValueError("Snapshot lease fields do not match its schema.")
        if value.get("schema") != SNAPSHOT_LEASE_SCHEMA:
            raise ValueError("Snapshot lease schema is unsupported.")
        source_sha256 = value.get("source_sha256")
        if source_sha256 is not None and (
            not isinstance(source_sha256, str)
            or not _SHA256_PATTERN.fullmatch(source_sha256)
        ):
            raise ValueError("Snapshot lease source SHA-256 is invalid.")
        source_size = value.get("source_size_bytes")
        if (
            isinstance(source_size, bool)
            or not isinstance(source_size, int)
            or source_size < 0
        ):
            raise ValueError("Snapshot lease source size is invalid.")
        owner_pid = value.get("owner_pid")
        if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0:
            raise ValueError("Snapshot lease owner PID is invalid.")
        owner_identity = value.get("owner_process_creation_identity")
        if owner_identity is not None and (
            not isinstance(owner_identity, str) or not owner_identity
        ):
            raise ValueError("Snapshot lease owner process identity is invalid.")
        created_at = value.get("created_at")
        app_version = value.get("app_version")
        state = value.get("lease_state")
        if not isinstance(created_at, str) or not created_at:
            raise ValueError("Snapshot lease creation time is invalid.")
        if not isinstance(app_version, str) or not app_version:
            raise ValueError("Snapshot lease application version is invalid.")
        if state not in {"capturing", "active", "released"}:
            raise ValueError("Snapshot lease state is invalid.")
        if state == "active" and source_sha256 is None:
            raise ValueError("An active snapshot lease requires a source SHA-256.")
        return cls(
            schema=SNAPSHOT_LEASE_SCHEMA,
            source_sha256=source_sha256,
            source_size_bytes=source_size,
            owner_pid=owner_pid,
            owner_process_creation_identity=owner_identity,
            created_at=created_at,
            app_version=app_version,
            lease_state=state,
        )


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_metadata(path: Path) -> SnapshotLeaseMetadata:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Snapshot lease could not be read: {exc}") from exc
    return SnapshotLeaseMetadata.from_dict(value)


def resolve_cache_root(
    project_path: Path | str | None = None,
    configured: Path | str | None = None,
) -> Path:
    """Resolve the configured cache or the project-local safe default."""

    selected: Path
    if configured is not None:
        selected = Path(configured).expanduser()
    else:
        environment = os.environ.get(CACHE_ENVIRONMENT_VARIABLE, "").strip()
        if environment:
            selected = Path(environment).expanduser()
        elif project_path is not None:
            project = Path(project_path).expanduser().resolve()
            selected = project.parent / ".groove-serpent" / "cache" / "snapshots"
        else:
            selected = Path.cwd() / ".groove-serpent" / "cache" / "snapshots"
    return selected.resolve()


def _owner_status(metadata: SnapshotLeaseMetadata) -> OwnerStatus:
    exists = _pid_exists(metadata.owner_pid)
    if exists is False:
        return "dead"
    if exists is None:
        return "unknown"
    recorded = metadata.owner_process_creation_identity
    if recorded is None:
        return "live"
    current = process_creation_identity(metadata.owner_pid)
    if current is None:
        return "unknown"
    if current != recorded:
        return "reused"
    return "live"


def _directory_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if not item.is_symlink() and item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _safe_lease_directory(root: Path, directory: Path) -> bool:
    try:
        junction_probe: Any = getattr(directory, "is_junction", None)
        if junction_probe is not None and bool(junction_probe()):
            return False
        attributes = getattr(directory.lstat(), "st_file_attributes", 0)
        if int(attributes) & 0x400:  # Windows FILE_ATTRIBUTE_REPARSE_POINT
            return False
        return (
            directory.parent.resolve() == root.resolve()
            and directory.name.startswith(SNAPSHOT_DIRECTORY_PREFIX)
            and not directory.is_symlink()
            and directory.is_dir()
        )
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class CacheEntryStatus:
    directory: Path
    bytes_on_disk: int
    metadata: SnapshotLeaseMetadata | None
    owner_status: OwnerStatus
    reclaimable: bool
    problem: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "directory": str(self.directory),
            "bytes_on_disk": self.bytes_on_disk,
            "metadata": self.metadata.to_dict() if self.metadata else None,
            "owner_status": self.owner_status,
            "reclaimable": self.reclaimable,
            "problem": self.problem,
        }


@dataclass(frozen=True, slots=True)
class CacheStatusReport:
    root: Path
    entries: tuple[CacheEntryStatus, ...]

    @property
    def total_bytes(self) -> int:
        return sum(entry.bytes_on_disk for entry in self.entries)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(
            entry.bytes_on_disk for entry in self.entries if entry.reclaimable
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "groove-serpent.cache-status/1",
            "root": str(self.root),
            "entries": [entry.to_dict() for entry in self.entries],
            "summary": {
                "entries": len(self.entries),
                "total_bytes": self.total_bytes,
                "reclaimable_entries": sum(
                    entry.reclaimable for entry in self.entries
                ),
                "reclaimable_bytes": self.reclaimable_bytes,
            },
        }


def inspect_snapshot_cache(root: Path | str) -> CacheStatusReport:
    """Inspect direct child leases without mutating or following links."""

    cache_root = Path(root).expanduser().resolve()
    if not cache_root.exists():
        return CacheStatusReport(cache_root, ())
    if not cache_root.is_dir():
        raise GrooveSerpentError(f"Snapshot cache root is not a directory: {cache_root}")
    entries: list[CacheEntryStatus] = []
    try:
        children = sorted(cache_root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise GrooveSerpentError(f"Snapshot cache could not be inspected: {exc}") from exc
    for directory in children:
        if not directory.name.startswith(SNAPSHOT_DIRECTORY_PREFIX):
            continue
        safe_directory = _safe_lease_directory(cache_root, directory)
        size = _directory_size(directory) if safe_directory else 0
        if not safe_directory:
            entries.append(
                CacheEntryStatus(
                    directory=directory,
                    bytes_on_disk=size,
                    metadata=None,
                    owner_status="unknown",
                    reclaimable=False,
                    problem="Unsafe or non-directory cache entry was ignored.",
                )
            )
            continue
        try:
            metadata = _load_metadata(directory / SNAPSHOT_LEASE_FILENAME)
        except ValueError as exc:
            entries.append(
                CacheEntryStatus(
                    directory=directory,
                    bytes_on_disk=size,
                    metadata=None,
                    owner_status="unknown",
                    reclaimable=False,
                    problem=str(exc),
                )
            )
            continue
        owner_status = _owner_status(metadata)
        entries.append(
            CacheEntryStatus(
                directory=directory,
                bytes_on_disk=size,
                metadata=metadata,
                owner_status=owner_status,
                reclaimable=owner_status in {"dead", "reused"},
            )
        )
    return CacheStatusReport(cache_root, tuple(entries))


@dataclass(frozen=True, slots=True)
class CacheCleanupReport:
    root: Path
    removed: tuple[Path, ...]
    removed_bytes: int
    skipped_live: int
    skipped_unknown: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "groove-serpent.cache-cleanup/1",
            "root": str(self.root),
            "removed": [str(path) for path in self.removed],
            "removed_bytes": self.removed_bytes,
            "skipped_live": self.skipped_live,
            "skipped_unknown": self.skipped_unknown,
        }


def cleanup_stale_snapshots(root: Path | str) -> CacheCleanupReport:
    """Remove only leases whose recorded owner is provably no longer live."""

    initial = inspect_snapshot_cache(root)
    removed: list[Path] = []
    removed_bytes = 0
    skipped_live = 0
    skipped_unknown = 0
    for entry in initial.entries:
        metadata = entry.metadata
        if metadata is None:
            skipped_unknown += 1
            continue
        if not entry.reclaimable:
            if entry.owner_status == "live":
                skipped_live += 1
            else:
                skipped_unknown += 1
            continue
        # Close the inspection/deletion race: validate the exact receipt and
        # owner status again immediately before the directory is removed.
        if not _safe_lease_directory(initial.root, entry.directory):
            skipped_unknown += 1
            continue
        try:
            current = _load_metadata(entry.directory / SNAPSHOT_LEASE_FILENAME)
        except ValueError:
            skipped_unknown += 1
            continue
        if current != metadata:
            skipped_unknown += 1
            continue
        owner_status = _owner_status(current)
        if owner_status not in {"dead", "reused"}:
            if owner_status == "live":
                skipped_live += 1
            else:
                skipped_unknown += 1
            continue
        try:
            shutil.rmtree(entry.directory)
        except OSError:
            skipped_unknown += 1
            continue
        removed.append(entry.directory)
        removed_bytes += entry.bytes_on_disk
    return CacheCleanupReport(
        root=initial.root,
        removed=tuple(removed),
        removed_bytes=removed_bytes,
        skipped_live=skipped_live,
        skipped_unknown=skipped_unknown,
    )


@dataclass(slots=True)
class SnapshotLease:
    """One live process's exclusive ownership of a snapshot directory."""

    root: Path
    directory: Path
    receipt_path: Path
    metadata: SnapshotLeaseMetadata
    _released: bool = False

    def assert_owned(self) -> None:
        if self._released:
            raise GrooveSerpentError("The snapshot lease has already been released.")
        if not _safe_lease_directory(self.root, self.directory):
            raise GrooveSerpentError("The snapshot lease directory changed unexpectedly.")
        try:
            observed = _load_metadata(self.receipt_path)
        except ValueError as exc:
            raise GrooveSerpentError("The snapshot lease receipt changed unexpectedly.") from exc
        if observed != self.metadata:
            raise GrooveSerpentError("The snapshot lease receipt changed unexpectedly.")
        if observed.owner_pid != os.getpid():
            raise GrooveSerpentError("The snapshot lease belongs to another process.")
        expected_identity = observed.owner_process_creation_identity
        current_identity = process_creation_identity()
        if expected_identity is not None and current_identity != expected_identity:
            raise GrooveSerpentError("The snapshot lease process identity changed.")

    def bind_source_identity(self, source_sha256: str, source_size_bytes: int) -> None:
        """Atomically promote a provisional capture to an active verified lease."""

        self.assert_owned()
        if self.metadata.lease_state != "capturing":
            raise GrooveSerpentError("Only a capturing snapshot lease can be bound.")
        normalized_hash = source_sha256.strip().lower()
        if not _SHA256_PATTERN.fullmatch(normalized_hash):
            raise GrooveSerpentError("Snapshot lease source SHA-256 is invalid.")
        size = _strict_nonnegative_integer(source_size_bytes, "Snapshot source size")
        bound = replace(
            self.metadata,
            source_sha256=normalized_hash,
            source_size_bytes=size,
            lease_state="active",
        )
        _write_json_atomic(self.receipt_path, bound.to_dict())
        self.metadata = bound

    def release(self, *, remove: bool = True) -> None:
        """Mark the lease released and optionally remove its directory."""

        if self._released:
            return
        if not self.directory.exists():
            self._released = True
            return
        self.assert_owned()
        released = replace(self.metadata, lease_state="released")
        _write_json_atomic(self.receipt_path, released.to_dict())
        self.metadata = released
        self._released = True
        if remove and _safe_lease_directory(self.root, self.directory):
            shutil.rmtree(self.directory)

    def cleanup(self) -> None:
        """Compatibility cleanup hook for snapshot context managers."""

        self.release(remove=True)


def acquire_snapshot_lease(
    cache_root: Path | str,
    *,
    source_sha256: str,
    source_size_bytes: int,
) -> SnapshotLease:
    """Create an active, uniquely named snapshot workspace and receipt."""

    normalized_hash = source_sha256.strip().lower()
    if not _SHA256_PATTERN.fullmatch(normalized_hash):
        raise GrooveSerpentError("Snapshot lease source SHA-256 is invalid.")
    size = _strict_nonnegative_integer(source_size_bytes, "Snapshot source size")
    root = Path(cache_root).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GrooveSerpentError(f"Snapshot cache could not be created: {exc}") from exc
    if not root.is_dir():
        raise GrooveSerpentError(f"Snapshot cache root is not a directory: {root}")
    directory = Path(
        tempfile.mkdtemp(prefix=SNAPSHOT_DIRECTORY_PREFIX, dir=str(root))
    ).resolve()
    receipt_path = directory / SNAPSHOT_LEASE_FILENAME
    metadata = SnapshotLeaseMetadata(
        schema=SNAPSHOT_LEASE_SCHEMA,
        source_sha256=normalized_hash,
        source_size_bytes=size,
        owner_pid=os.getpid(),
        owner_process_creation_identity=process_creation_identity(),
        created_at=_utc_now_iso(),
        app_version=__version__,
        lease_state="active",
    )
    try:
        _write_json_atomic(receipt_path, metadata.to_dict())
        return SnapshotLease(root, directory, receipt_path, metadata)
    except BaseException:
        if _safe_lease_directory(root, directory):
            shutil.rmtree(directory)
        raise


def acquire_provisional_snapshot_lease(
    cache_root: Path | str,
    *,
    source_size_bytes: int,
) -> SnapshotLease:
    """Create a lease before a single-pass capture has computed its digest."""

    size = _strict_nonnegative_integer(source_size_bytes, "Snapshot source size")
    root = Path(cache_root).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GrooveSerpentError(f"Snapshot cache could not be created: {exc}") from exc
    if not root.is_dir():
        raise GrooveSerpentError(f"Snapshot cache root is not a directory: {root}")
    directory = Path(
        tempfile.mkdtemp(prefix=SNAPSHOT_DIRECTORY_PREFIX, dir=str(root))
    ).resolve()
    receipt_path = directory / SNAPSHOT_LEASE_FILENAME
    metadata = SnapshotLeaseMetadata(
        schema=SNAPSHOT_LEASE_SCHEMA,
        source_sha256=None,
        source_size_bytes=size,
        owner_pid=os.getpid(),
        owner_process_creation_identity=process_creation_identity(),
        created_at=_utc_now_iso(),
        app_version=__version__,
        lease_state="capturing",
    )
    try:
        _write_json_atomic(receipt_path, metadata.to_dict())
        return SnapshotLease(root, directory, receipt_path, metadata)
    except BaseException:
        if _safe_lease_directory(root, directory):
            shutil.rmtree(directory)
        raise


@dataclass(frozen=True, slots=True)
class StoragePreflight:
    path: Path
    required_bytes: int
    reserve_bytes: int
    available_bytes: int

    @property
    def total_needed_bytes(self) -> int:
        return self.required_bytes + self.reserve_bytes


def _storage_anchor(destination: Path) -> Path:
    candidate = destination.expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    if not candidate.exists():
        raise GrooveSerpentError(
            f"No existing filesystem ancestor was found for storage path: {destination}"
        )
    return candidate


def ensure_free_space(
    destination: Path | str,
    required_bytes: int,
    *,
    label: str,
    reserve_bytes: int = DEFAULT_STORAGE_RESERVE_BYTES,
) -> StoragePreflight:
    """Fail before a large write when the target filesystem is too full."""

    required = _strict_nonnegative_integer(required_bytes, "Required storage")
    reserve = _strict_nonnegative_integer(reserve_bytes, "Storage reserve")
    path = Path(destination).expanduser().resolve()
    anchor = _storage_anchor(path)
    try:
        available = int(shutil.disk_usage(anchor).free)
    except OSError as exc:
        raise GrooveSerpentError(
            f"{label} storage availability could not be checked: {exc}"
        ) from exc
    result = StoragePreflight(path, required, reserve, available)
    if result.total_needed_bytes > available:
        raise GrooveSerpentError(
            f"{label} requires {required} bytes plus {reserve} bytes of reserve "
            f"({result.total_needed_bytes} bytes total), but only {available} bytes "
            f"are available at {anchor}."
        )
    return result


__all__ = [
    "CACHE_ENVIRONMENT_VARIABLE",
    "CacheCleanupReport",
    "CacheEntryStatus",
    "CacheStatusReport",
    "DEFAULT_STORAGE_RESERVE_BYTES",
    "SNAPSHOT_DIRECTORY_PREFIX",
    "SNAPSHOT_LEASE_FILENAME",
    "SNAPSHOT_LEASE_SCHEMA",
    "SnapshotLease",
    "SnapshotLeaseMetadata",
    "StoragePreflight",
    "acquire_provisional_snapshot_lease",
    "acquire_snapshot_lease",
    "cleanup_stale_snapshots",
    "ensure_free_space",
    "inspect_snapshot_cache",
    "process_creation_identity",
    "resolve_cache_root",
]
