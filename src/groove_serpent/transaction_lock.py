"""Cross-process write leases for mutable Groove Serpent project files.

The in-process review-server mutex prevents two requests in one server from
overlapping, but it cannot coordinate a second server or CLI process.  This
module adds one small, persistent sibling lock file per mutable target and uses
the operating system's advisory byte-range/file lock for the duration of the
complete compare-and-swap save.

The lock never authorizes a write.  Callers must still re-read and compare the
target revision and identity after acquiring it. Native Windows and WSL/Linux
processes must not concurrently mutate one project tree: their advisory lock
families are not proven interoperable, so mixed Windows/WSL access is
unsupported and atomic-create capability is probed before every lease.
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .atomic_create import probe_atomic_no_replace, rename_no_replace
from .errors import ProjectValidationError
from .portable_names import portable_relative_path_key


_LOCK_MAGIC = b"groove-serpent exclusive target lock/1\n"
_LOCK_PREFIX = ".groove-serpent-write-"
_LOCK_SUFFIX = ".lock"
_REPARSE_POINT = 0x400
_DEFAULT_TIMEOUT_SECONDS = 10.0
_RETRY_SECONDS = 0.01


@dataclass(frozen=True, slots=True)
class _LockIdentity:
    device: int
    inode: int
    file_type: int
    link_count: int
    size: int
    birth_ns: int | None
    file_attributes: int | None

    @classmethod
    def capture(cls, value: os.stat_result) -> _LockIdentity:
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            file_type=stat.S_IFMT(value.st_mode),
            link_count=int(value.st_nlink),
            size=int(value.st_size),
            birth_ns=(
                int(birth) if (birth := getattr(value, "st_birthtime_ns", None))
                is not None
                else None
            ),
            file_attributes=(
                int(attributes)
                if (attributes := getattr(value, "st_file_attributes", None))
                is not None
                else None
            ),
        )


@dataclass(slots=True)
class TargetWriteLease:
    """One held OS lock whose sibling path identity can be reasserted."""

    path: Path
    descriptor: int
    identity: _LockIdentity

    def assert_current(self) -> None:
        """Fail if the held lock file's path was substituted."""

        try:
            opened = os.fstat(self.descriptor)
            current = self.path.lstat()
        except OSError as exc:
            raise ProjectValidationError(
                "The project write-lock identity could not be rechecked."
            ) from exc
        if _is_reparse(current) or stat.S_ISLNK(current.st_mode):
            raise ProjectValidationError(
                "The project write-lock path became a link or reparse point."
            )
        if (
            _LockIdentity.capture(opened) != self.identity
            or _LockIdentity.capture(current) != self.identity
        ):
            raise ProjectValidationError(
                "The project write-lock identity changed during the save."
            )
        _validate_magic(self.descriptor)


@dataclass(frozen=True, slots=True)
class _TargetIdentity:
    device: int
    inode: int
    mode: int
    link_count: int
    size: int
    modified_ns: int
    changed_ns: int
    birth_ns: int | None
    file_attributes: int | None

    @classmethod
    def capture(cls, value: os.stat_result) -> _TargetIdentity:
        birth = getattr(value, "st_birthtime_ns", None)
        attributes = getattr(value, "st_file_attributes", None)
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            mode=int(value.st_mode),
            link_count=int(value.st_nlink),
            size=int(value.st_size),
            modified_ns=int(value.st_mtime_ns),
            changed_ns=int(value.st_ctime_ns),
            birth_ns=int(birth) if birth is not None else None,
            file_attributes=int(attributes) if attributes is not None else None,
        )


def _is_reparse(value: os.stat_result) -> bool:
    return bool(int(getattr(value, "st_file_attributes", 0)) & _REPARSE_POINT)


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _windows_long_path(path: Path) -> Path:
    ctypes: Any = importlib.import_module("ctypes")
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    get_long_path_name: Any = kernel32.GetLongPathNameW
    required = int(get_long_path_name(os.fspath(path), None, 0))
    if required <= 0:
        raise ProjectValidationError(
            "Windows could not canonicalize the mutable target path."
        )
    buffer: Any = ctypes.create_unicode_buffer(required + 1)
    written = int(get_long_path_name(os.fspath(path), buffer, len(buffer)))
    if written <= 0 or written >= len(buffer):
        raise ProjectValidationError(
            "Windows could not canonicalize the mutable target path."
        )
    return Path(str(buffer.value))


def canonical_target_path(target: Path) -> Path:
    """Canonicalize ancestors and a verified plain existing final component.

    An absent final name remains literal.  An existing final entry is checked
    without following it before and after Windows 8.3/long-name expansion (or
    the native equivalent), so a symlink, junction, or reparse point is never
    accepted as a mutable project target.
    """

    absolute = _absolute_without_resolving(target)
    try:
        parent = absolute.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectValidationError(
            f"Mutable-target parent folder is not available: {absolute.parent}"
        ) from exc
    candidate = parent / absolute.name
    if not os.path.lexists(candidate):
        return candidate
    try:
        before = candidate.lstat()
    except OSError as exc:
        raise ProjectValidationError(
            f"Mutable target is not available: {candidate.name}"
        ) from exc
    if (
        candidate.is_symlink()
        or _is_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or int(before.st_nlink) != 1
    ):
        raise ProjectValidationError(
            "Mutable target must be a single-link regular, non-reparse file: "
            f"{candidate.name}"
        )
    try:
        canonical = (
            _windows_long_path(candidate)
            if os.name == "nt"
            else candidate.resolve(strict=True)
        )
        repeated = candidate.lstat()
        canonical_value = canonical.lstat()
    except ProjectValidationError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ProjectValidationError(
            "Mutable-target identity changed during path canonicalization."
        ) from exc
    expected = _TargetIdentity.capture(before)
    if (
        _TargetIdentity.capture(repeated) != expected
        or _TargetIdentity.capture(canonical_value) != expected
        or canonical.is_symlink()
        or _is_reparse(canonical_value)
        or not stat.S_ISREG(canonical_value.st_mode)
        or int(canonical_value.st_nlink) != 1
    ):
        raise ProjectValidationError(
            "Mutable-target identity changed during path canonicalization."
        )
    return canonical


def target_lock_path(target: Path) -> Path:
    """Return a bounded portable-equivalence-aware sibling lock name.

    Portable output rules treat NFC/casefold-equivalent spellings as one
    destination even on filesystems that permit both entries.  They therefore
    must also share one write lease; otherwise two creators can both pass their
    portable-name preflight and commit parallel names.
    """

    absolute = canonical_target_path(target)
    normalized = portable_relative_path_key(
        os.path.normpath(os.fspath(absolute))
    )
    digest = hashlib.sha256(os.fsencode(normalized)).hexdigest()[:32]
    return absolute.parent / f"{_LOCK_PREFIX}{digest}{_LOCK_SUFFIX}"


def _write_new_lock_file(path: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f"{_LOCK_PREFIX}new-",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_LOCK_MAGIC)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            rename_no_replace(temporary, path)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ProjectValidationError(
                "The filesystem cannot atomically create a project write lock."
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_lock_file(path: Path) -> None:
    if not os.path.lexists(path):
        _write_new_lock_file(path)


def _open_lock_file(path: Path) -> tuple[int, _LockIdentity]:
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ProjectValidationError(
                    "The project write-lock path must be a single-link regular, "
                    "non-reparse file."
                ) from exc
            raise
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            _is_reparse(current)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or int(current.st_nlink) != 1
            or int(opened.st_nlink) != 1
        ):
            raise ProjectValidationError(
                "The project write-lock path must be a single-link regular, "
                "non-reparse file."
            )
        opened_identity = _LockIdentity.capture(opened)
        if opened_identity != _LockIdentity.capture(current):
            raise ProjectValidationError(
                "The project write-lock path changed while it was opened."
            )
        return descriptor, opened_identity
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _try_lock(descriptor: int) -> bool:
    if os.name == "nt":
        msvcrt: Any = importlib.import_module("msvcrt")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, len(_LOCK_MAGIC))
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EPERM}:
                return False
            raise
        return True
    fcntl: Any = importlib.import_module("fcntl")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise
    return True


def _unlock(descriptor: int) -> None:
    if os.name == "nt":
        msvcrt: Any = importlib.import_module("msvcrt")
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, len(_LOCK_MAGIC))
        return
    fcntl: Any = importlib.import_module("fcntl")
    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _validate_magic(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    raw = os.read(descriptor, len(_LOCK_MAGIC) + 1)
    if raw != _LOCK_MAGIC:
        raise ProjectValidationError(
            "The reserved project write-lock file has unexpected contents."
        )


@contextmanager
def exclusive_target_write_lease(
    target: Path,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> Iterator[TargetWriteLease]:
    """Serialize one target's complete read/compare/write transaction."""

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0.0 <= float(timeout_seconds) <= 300.0
    ):
        raise ProjectValidationError(
            "Project write-lock timeout must be a finite value from 0 to 300 seconds."
        )
    timeout = float(timeout_seconds)
    if not timeout < float("inf"):
        raise ProjectValidationError(
            "Project write-lock timeout must be a finite value from 0 to 300 seconds."
        )
    absolute = _absolute_without_resolving(target)
    absolute.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target_lock_path(absolute)
    try:
        exercised = probe_atomic_no_replace(lock_path.parent)
    except OSError as exc:
        raise ProjectValidationError(
            "This filesystem does not provide the atomic no-replace operation "
            "required for project write leases. Mixed Windows/WSL access is "
            "unsupported."
        ) from exc
    try:
        same_parent = exercised.samefile(lock_path.parent)
    except OSError as exc:
        raise ProjectValidationError(
            "The project write-lock directory changed during its capability probe."
        ) from exc
    if not same_parent:
        raise ProjectValidationError(
            "The atomic no-replace probe exercised a different directory."
        )
    _ensure_lock_file(lock_path)
    descriptor, identity = _open_lock_file(lock_path)
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while not acquired:
            try:
                acquired = _try_lock(descriptor)
            except OSError as exc:
                raise ProjectValidationError(
                    "The operating system could not acquire the project write lock."
                ) from exc
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise ProjectValidationError(
                    "Another Groove Serpent process is writing this project; retry after "
                    "that save finishes."
                )
            time.sleep(_RETRY_SECONDS)
        _validate_magic(descriptor)
        lease = TargetWriteLease(lock_path, descriptor, identity)
        lease.assert_current()
        yield lease
        lease.assert_current()
    finally:
        if acquired:
            try:
                _unlock(descriptor)
            except OSError:
                pass
        os.close(descriptor)


__all__ = [
    "TargetWriteLease",
    "canonical_target_path",
    "exclusive_target_write_lease",
    "target_lock_path",
]
