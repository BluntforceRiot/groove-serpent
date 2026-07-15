"""Descriptor-bound staging for migration replacements.

Migration journals intentionally keep deterministic recovery artifacts, but a
deterministic pathname must never be the final authority for bytes committed to
the live project.  This module copies already-validated bytes into a random,
exclusive sibling, keeps that file open with delete sharing where the platform
supports it, and lets callers verify that the exact open file became the live
target after their atomic rename.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .atomic_create import rename_no_replace
from .errors import ProjectValidationError


def _is_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    links: int
    modified_ns: int
    file_attributes: int

    @classmethod
    def capture(cls, value: os.stat_result) -> "FileIdentity":
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            mode=int(value.st_mode),
            size=int(value.st_size),
            links=int(value.st_nlink),
            modified_ns=int(value.st_mtime_ns),
            file_attributes=int(getattr(value, "st_file_attributes", 0)),
        )


def _require_plain_file(value: os.stat_result, path: Path) -> FileIdentity:
    identity = FileIdentity.capture(value)
    if (
        _is_reparse(value)
        or not stat.S_ISREG(value.st_mode)
        or identity.links != 1
    ):
        raise ProjectValidationError(
            "Migration replacement refuses a linked, non-regular, or "
            f"reparse-point file: {path.name}"
        )
    return identity


def _open_windows_shared_delete(
    path: Path, *, delete_access: bool = False
) -> BinaryIO:
    """Open without following a reparse point and permit atomic replacement."""

    import msvcrt
    from ctypes import wintypes

    loader: Any = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise OSError("Windows file-handle APIs are unavailable.")
    kernel32: Any = loader("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        str(path),
        0x80000000 | (0x00010000 if delete_access else 0),
        # GENERIC_READ | optional DELETE
        0x00000001 | 0x00000002 | 0x00000004,  # read/write/delete sharing
        None,
        3,  # OPEN_EXISTING
        0x00200000 | 0x00000080,  # OPEN_REPARSE_POINT | ATTRIBUTE_NORMAL
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in {None, 0, invalid}:
        get_last_error: Any = getattr(ctypes, "get_last_error", None)
        error = int(get_last_error()) if get_last_error is not None else 0
        raise OSError(error, os.strerror(error), str(path))
    try:
        open_osfhandle: Any = getattr(msvcrt, "open_osfhandle", None)
        if open_osfhandle is None:
            raise OSError("Windows descriptor conversion is unavailable.")
        descriptor = open_osfhandle(
            int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    return os.fdopen(descriptor, "rb")


def _open_bound_read(path: Path, *, delete_access: bool = False) -> BinaryIO:
    if os.name == "nt":
        return _open_windows_shared_delete(path, delete_access=delete_access)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    return os.fdopen(descriptor, "rb")


def _bound_payload_matches(
    handle: BinaryIO,
    identity: FileIdentity,
    path: Path,
    payload: bytes,
    maximum: int,
) -> bool:
    """Bind one pathname and payload to an already-open regular file."""

    try:
        held = _require_plain_file(os.fstat(handle.fileno()), path)
        current = _require_plain_file(path.lstat(), path)
        if path.is_symlink() or held != identity or current != identity:
            return False
        handle.seek(0)
        raw = handle.read(maximum + 1)
        handle.seek(0)
    except (FileNotFoundError, OSError, ProjectValidationError):
        return False
    return raw == payload


def quarantine_path_no_replace(path: Path, *, purpose: str) -> Path:
    """Move the current path to a random sibling without replacing anything.

    The move is the ownership-transfer boundary.  It never destroys whichever
    object currently occupies ``path``.  A caller must verify the quarantined
    object before deciding whether it owns and may destroy it.
    """

    for _ in range(32):
        token = secrets.token_hex(16)
        name_seed = f"{path.name}\0{purpose}".encode(
            "utf-8", errors="surrogatepass"
        )
        name_id = hashlib.sha256(name_seed).hexdigest()[:24]
        quarantine = path.with_name(
            f".groove-serpent-quarantine-{name_id}-{token}.preserved"
        )
        try:
            rename_no_replace(path, quarantine)
        except FileExistsError:
            continue
        return quarantine
    raise ProjectValidationError(
        f"Could not allocate an exclusive {purpose} quarantine sibling."
    )


class _WindowsFileDispositionInfo(ctypes.Structure):
    _fields_ = [("delete_file", ctypes.c_int)]


def _delete_bound_windows(handle: BinaryIO) -> bool:
    """Mark the exact open Windows object for deletion by handle."""

    import msvcrt
    from ctypes import wintypes

    loader: Any = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return False
    kernel32: Any = loader("kernel32", use_last_error=True)
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    get_osfhandle: Any = getattr(msvcrt, "get_osfhandle", None)
    if get_osfhandle is None:
        return False
    native_handle = int(get_osfhandle(handle.fileno()))
    disposition = _WindowsFileDispositionInfo(1)
    return bool(
        kernel32.SetFileInformationByHandle(
            native_handle,
            4,  # FileDispositionInfo
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        )
    )


def _remove_bound_file(
    path: Path,
    handle: BinaryIO,
    identity: FileIdentity,
    payload: bytes,
    maximum: int,
    *,
    purpose: str,
    missing_ok: bool,
) -> tuple[bool, Path | None]:
    """Quarantine and remove only the exact object held by ``handle``.

    The original public pathname is moved before any destructive operation.
    If a rename race transfers a different object, that object is preserved at
    the returned quarantine path and the caller must fail closed.
    """

    if not _bound_payload_matches(handle, identity, path, payload, maximum):
        if missing_ok and not os.path.lexists(path):
            handle.close()
            return True, None
        handle.close()
        return False, path
    try:
        quarantine = quarantine_path_no_replace(path, purpose=purpose)
    except FileNotFoundError:
        handle.close()
        return (True, None) if missing_ok else (False, path)
    except (OSError, ProjectValidationError):
        # Sharing violations from an indexer/backup reader and quarantine
        # allocation failures must fail closed without leaking this handle or
        # replacing the caller's primary migration exception.
        handle.close()
        return False, path
    if not _bound_payload_matches(
        handle, identity, quarantine, payload, maximum
    ):
        handle.close()
        return False, quarantine

    if os.name == "nt":
        deleted = _delete_bound_windows(handle)
        handle.close()
        if not deleted:
            # Prepared replacements deliberately hold only GENERIC_READ while
            # they may become the public target.  A DELETE-capable handle
            # prevents ordinary Windows readers (which do not share delete
            # access) from opening that installed target.  Once the exact
            # object has been moved to its random quarantine, bind it again
            # briefly with DELETE access and re-verify identity and bytes.
            delete_handle: BinaryIO | None = None
            try:
                delete_handle = _open_bound_read(
                    quarantine, delete_access=True
                )
                if not _bound_payload_matches(
                    delete_handle,
                    identity,
                    quarantine,
                    payload,
                    maximum,
                ):
                    delete_handle.close()
                    return False, quarantine
                deleted = _delete_bound_windows(delete_handle)
            except (OSError, ProjectValidationError, FileNotFoundError):
                deleted = False
            finally:
                if delete_handle is not None and not delete_handle.closed:
                    delete_handle.close()
        if not deleted or os.path.lexists(quarantine):
            return False, quarantine
        return True, None

    try:
        quarantine.unlink()
        unlinked = int(os.fstat(handle.fileno()).st_nlink) == 0
    except OSError:
        unlinked = False
    finally:
        handle.close()
    if not unlinked or os.path.lexists(quarantine):
        return False, quarantine
    return True, None


def remove_exact_plain_file(
    path: Path,
    expected_sha256: str,
    *,
    maximum: int,
    purpose: str,
    expected_identity: FileIdentity | None = None,
) -> None:
    """Remove one exact single-link file or preserve a conflicting object."""

    handle: BinaryIO | None = None
    try:
        before = _require_plain_file(path.lstat(), path)
        if path.is_symlink():
            raise ProjectValidationError(
                f"{purpose} refuses a symbolic link: {path.name}"
            )
        handle = _open_bound_read(path, delete_access=os.name == "nt")
        opened = _require_plain_file(os.fstat(handle.fileno()), path)
        raw = handle.read(maximum + 1)
        handle.seek(0)
        after = _require_plain_file(path.lstat(), path)
    except FileNotFoundError:
        if handle is not None:
            handle.close()
        raise
    except ProjectValidationError:
        if handle is not None:
            handle.close()
        raise
    except OSError as exc:
        if handle is not None:
            handle.close()
        raise ProjectValidationError(
            f"{purpose} could not safely bind {path.name}: {exc}"
        ) from exc
    if handle is None:
        raise ProjectValidationError(f"{purpose} did not acquire a file handle.")
    if (
        before != opened
        or opened != after
        or (expected_identity is not None and opened != expected_identity)
    ):
        handle.close()
        raise ProjectValidationError(
            f"{purpose} file identity changed while binding {path.name}."
        )
    if len(raw) > maximum or hashlib.sha256(raw).hexdigest() != expected_sha256:
        handle.close()
        raise ProjectValidationError(
            f"{purpose} content changed before quarantine: {path.name}"
        )
    removed, conflict = _remove_bound_file(
        path,
        handle,
        opened,
        raw,
        maximum,
        purpose=purpose,
        missing_ok=False,
    )
    if not removed:
        location = conflict.name if conflict is not None else path.name
        raise ProjectValidationError(
            f"{purpose} lost exact-object ownership; preserved conflict: {location}"
        )


def read_plain_bound(path: Path, maximum: int) -> tuple[bytes, FileIdentity]:
    """Read one single-link regular file through a stable open descriptor."""

    try:
        before = _require_plain_file(path.lstat(), path)
        if path.is_symlink():
            raise ProjectValidationError(
                f"Migration replacement refuses a symbolic link: {path.name}"
            )
        with _open_bound_read(path) as handle:
            opened = _require_plain_file(os.fstat(handle.fileno()), path)
            raw = handle.read(maximum + 1)
        after = _require_plain_file(path.lstat(), path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ProjectValidationError(
            f"Migration replacement could not safely read {path.name}: {exc}"
        ) from exc
    if before != opened or opened != after:
        raise ProjectValidationError(
            f"Migration replacement file identity changed while reading {path.name}."
        )
    if len(raw) > maximum:
        raise ProjectValidationError(
            f"Migration replacement file exceeds its {maximum}-byte limit: {path.name}"
        )
    return raw, opened


@dataclass(slots=True)
class PreparedReplacement:
    path: Path
    handle: BinaryIO
    identity: FileIdentity
    payload: bytes
    maximum: int

    def matches_target(self, target: Path) -> bool:
        """Return whether the exact still-open stage is now the target."""

        try:
            held = _require_plain_file(os.fstat(self.handle.fileno()), target)
            raw, target_identity = read_plain_bound(target, self.maximum)
        except (OSError, ProjectValidationError, FileNotFoundError):
            return False
        return (
            held == self.identity
            and target_identity == self.identity
            and raw == self.payload
        )

    def discard(self) -> Path | None:
        """Discard the held stage and return any path retained for inspection."""

        if self.handle.closed:
            return self.path if os.path.lexists(self.path) else None
        removed, retained = _remove_bound_file(
            self.path,
            self.handle,
            self.identity,
            self.payload,
            self.maximum,
            purpose="prepared-stage-cleanup",
            missing_ok=True,
        )
        return None if removed else (retained or self.path)


def prepare_replacement(
    target: Path,
    payload: bytes,
    *,
    maximum: int,
    purpose: str,
) -> PreparedReplacement:
    """Create and bind a randomized exclusive sibling containing exact bytes."""

    if len(payload) > maximum:
        raise ProjectValidationError(
            f"{purpose} exceeds its {maximum}-byte migration limit."
        )
    name_seed = f"{target.name}\0{purpose}".encode(
        "utf-8", errors="surrogatepass"
    )
    name_id = hashlib.sha256(name_seed).hexdigest()[:24]
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".groove-serpent-stage-{name_id}-",
        suffix=".tmp",
    )
    stage = Path(temporary_name)
    written_identity: FileIdentity | None = None
    handle: BinaryIO | None = None
    try:
        writer = os.fdopen(descriptor, "wb")
        descriptor = -1
        with writer:
            writer.write(payload)
            writer.flush()
            os.fsync(writer.fileno())
            written_identity = _require_plain_file(os.fstat(writer.fileno()), stage)
        handle = _open_bound_read(stage)
        identity = _require_plain_file(os.fstat(handle.fileno()), stage)
        observed = handle.read(maximum + 1)
        handle.seek(0)
        path_identity = _require_plain_file(stage.lstat(), stage)
        if identity != path_identity or observed != payload:
            raise ProjectValidationError(
                f"{purpose} changed while its replacement handle was bound."
            )
        return PreparedReplacement(stage, handle, identity, payload, maximum)
    except BaseException as exc:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        retained: Path | None = None
        cleanup_failure: BaseException | None = None
        if handle is not None and written_identity is not None:
            try:
                removed, conflict = _remove_bound_file(
                    stage,
                    handle,
                    written_identity,
                    payload,
                    maximum,
                    purpose="failed-stage-cleanup",
                    missing_ok=True,
                )
                if not removed:
                    retained = conflict or stage
            except BaseException as cleanup_exc:
                cleanup_failure = cleanup_exc
                if not handle.closed:
                    handle.close()
                if os.path.lexists(stage):
                    retained = stage
        elif handle is not None:
            handle.close()
            if os.path.lexists(stage):
                retained = stage
        elif written_identity is not None:
            try:
                remove_exact_plain_file(
                    stage,
                    hashlib.sha256(payload).hexdigest(),
                    maximum=maximum,
                    purpose="failed-stage-cleanup",
                    expected_identity=written_identity,
                )
            except FileNotFoundError:
                pass
            except BaseException as cleanup_exc:
                cleanup_failure = cleanup_exc
                if os.path.lexists(stage):
                    retained = stage
        if retained is not None:
            exc.add_note(
                "Groove Serpent retained a failed migration stage for "
                f"inspection: {retained.name}"
            )
        if cleanup_failure is not None:
            exc.add_note(
                "Stage cleanup also failed closed: "
                f"{type(cleanup_failure).__name__}: {cleanup_failure}"
            )
        raise


__all__ = [
    "PreparedReplacement",
    "prepare_replacement",
    "quarantine_path_no_replace",
    "read_plain_bound",
    "remove_exact_plain_file",
]
