"""Atomic, no-replace publication of an already-fsynced temporary file."""

from __future__ import annotations

import ctypes
import errno
import importlib
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn


_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_UNSUPPORTED_NO_REPLACE_ERRNOS = {
    errno.ENOSYS,
    errno.ENOTSUP,
    errno.EOPNOTSUPP,
}


@dataclass(frozen=True, slots=True)
class _OwnedProbeIdentity:
    """Stable attributes that bind cleanup to the probe file we created."""

    device: int
    inode: int
    mode: int
    link_count: int
    size: int
    modified_ns: int
    birth_ns: int | None
    file_attributes: int | None

    @classmethod
    def capture(cls, value: os.stat_result) -> _OwnedProbeIdentity:
        birth = getattr(value, "st_birthtime_ns", None)
        attributes = getattr(value, "st_file_attributes", None)
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            mode=int(value.st_mode),
            link_count=int(value.st_nlink),
            size=int(value.st_size),
            modified_ns=int(value.st_mtime_ns),
            birth_ns=int(birth) if birth is not None else None,
            file_attributes=int(attributes) if attributes is not None else None,
        )


class _WindowsFileDispositionInfo(ctypes.Structure):
    _fields_ = [("delete_file", ctypes.c_int)]


def _encoded_path(path: Path) -> bytes:
    encoded = os.fsencode(path)
    if b"\x00" in encoded:
        raise ValueError("Atomic rename paths cannot contain embedded NUL bytes.")
    return encoded


def _raise_rename_error(error: int, destination: Path) -> NoReturn:
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error, os.strerror(error), destination)
    raise OSError(error, os.strerror(error), destination)


def _linux_rename_no_replace(source: Path, destination: Path) -> None:
    libc: Any = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(
            errno.ENOTSUP,
            "Atomic no-replace rename is unavailable on this Linux runtime.",
            destination,
        )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = int(
        renameat2(
            _AT_FDCWD,
            _encoded_path(source),
            _AT_FDCWD,
            _encoded_path(destination),
            _RENAME_NOREPLACE,
        )
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}:
        raise OSError(
            errno.ENOTSUP,
            "This Linux filesystem does not support atomic no-replace rename.",
            destination,
        )
    _raise_rename_error(error, destination)


def _darwin_rename_no_replace(source: Path, destination: Path) -> None:
    libc: Any = ctypes.CDLL(None, use_errno=True)
    renamex_np = getattr(libc, "renamex_np", None)
    if renamex_np is None:
        raise OSError(
            errno.ENOTSUP,
            "Atomic exclusive rename is unavailable on this macOS runtime.",
            destination,
        )
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    result = int(
        renamex_np(
            _encoded_path(source),
            _encoded_path(destination),
            _RENAME_EXCL,
        )
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
        raise OSError(
            errno.ENOTSUP,
            "This macOS volume does not support atomic exclusive rename.",
            destination,
        )
    _raise_rename_error(error, destination)


def rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename ``source`` only when ``destination`` is absent.

    The operation never publishes a second hardlink, so a crash cannot strand
    an otherwise valid mutable target with an ambiguous multi-link identity.
    Unsupported platforms or filesystems fail closed instead of falling back
    to a check-then-replace sequence.
    """

    if source.parent != destination.parent:
        raise ValueError("Atomic no-replace rename requires one parent directory.")
    if os.name == "nt":
        # Python's Windows rename uses MoveFile semantics and refuses an
        # existing destination, unlike POSIX rename.
        os.rename(source, destination)
        return
    if sys.platform.startswith("linux"):
        _linux_rename_no_replace(source, destination)
        return
    if sys.platform == "darwin":
        _darwin_rename_no_replace(source, destination)
        return
    raise OSError(
        errno.ENOTSUP,
        "Atomic no-replace rename is unsupported on this platform.",
        destination,
    )


# Cleanup must remain available when callers replace the public operation in
# a failure-injection test. Both names bind the same fail-closed primitive in
# production.
_rename_no_replace_for_cleanup = rename_no_replace


def _is_reparse(value: os.stat_result) -> bool:
    return bool(int(getattr(value, "st_file_attributes", 0)) & 0x400)


def _opened_probe_matches(
    descriptor: int,
    path: Path,
    identity: _OwnedProbeIdentity,
    payload: bytes,
) -> bool:
    """Verify one open descriptor and its current path without following links."""

    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
    except OSError:
        return False
    if (
        _OwnedProbeIdentity.capture(opened) != identity
        or _OwnedProbeIdentity.capture(current) != identity
        or path.is_symlink()
        or _is_reparse(current)
        or not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or int(opened.st_nlink) != 1
        or int(current.st_nlink) != 1
    ):
        return False
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = os.read(descriptor, len(payload) + 1)
    except OSError:
        return False
    return raw == payload


def _same_probe_object(
    first: _OwnedProbeIdentity,
    second: _OwnedProbeIdentity,
) -> bool:
    """Compare immutable cleanup-binding fields while bytes are being written."""

    return (
        first.device,
        first.inode,
        first.mode,
        first.link_count,
        first.birth_ns,
        first.file_attributes,
    ) == (
        second.device,
        second.inode,
        second.mode,
        second.link_count,
        second.birth_ns,
        second.file_attributes,
    )


def _refresh_written_probe(
    path: Path,
    initial_identity: _OwnedProbeIdentity,
    intended_payload: bytes,
) -> tuple[_OwnedProbeIdentity, bytes]:
    """Bind cleanup to the exact bytes left by completed or interrupted I/O."""

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        opened_before = _OwnedProbeIdentity.capture(os.fstat(descriptor))
        current_before_metadata = path.lstat()
        current_before = _OwnedProbeIdentity.capture(current_before_metadata)
        if (
            opened_before != current_before
            or not _same_probe_object(initial_identity, opened_before)
            or path.is_symlink()
            or _is_reparse(current_before_metadata)
            or not stat.S_ISREG(current_before.mode)
            or current_before.link_count != 1
        ):
            raise OSError(
                errno.EBUSY,
                "Atomic no-replace probe changed identity during write.",
                path,
            )
        chunks: list[bytes] = []
        remaining = len(intended_payload) + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        observed_payload = b"".join(chunks)
        opened_after = _OwnedProbeIdentity.capture(os.fstat(descriptor))
        current_after_metadata = path.lstat()
        current_after = _OwnedProbeIdentity.capture(current_after_metadata)
        if (
            opened_after != current_after
            or not _same_probe_object(initial_identity, opened_after)
            or path.is_symlink()
            or _is_reparse(current_after_metadata)
            or not stat.S_ISREG(current_after.mode)
            or current_after.link_count != 1
            or opened_before != opened_after
            or opened_after.size != len(observed_payload)
            or not intended_payload.startswith(observed_payload)
        ):
            raise OSError(
                errno.EBUSY,
                "Atomic no-replace probe changed during write verification.",
                path,
            )
        return opened_after, observed_payload
    finally:
        os.close(descriptor)


def _remove_owned_probe_path_windows(
    path: Path,
    identity: _OwnedProbeIdentity,
    payload: bytes,
) -> bool:
    """Delete the owned Windows object by handle, never an object swapped by name."""

    if not os.path.lexists(path):
        return True
    win_dll: Any = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        return False
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    create_file: Any = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    close_handle: Any = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    set_file_information: Any = kernel32.SetFileInformationByHandle
    set_file_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    set_file_information.restype = ctypes.c_int

    delete_access = 0x00010000
    generic_read = 0x80000000
    file_read_attributes = 0x00000080
    share_read_write_delete = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    open_reparse_point = 0x00200000
    handle = create_file(
        os.fspath(path),
        delete_access | generic_read | file_read_attributes,
        share_read_write_delete,
        None,
        open_existing,
        open_reparse_point,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in {None, invalid_handle}:
        return not os.path.lexists(path)

    msvcrt: Any = importlib.import_module("msvcrt")
    descriptor: int | None = None
    try:
        try:
            descriptor = int(
                msvcrt.open_osfhandle(
                    int(handle),
                    os.O_RDONLY | getattr(os, "O_BINARY", 0),
                )
            )
        except OSError:
            close_handle(handle)
            return False
        if not _opened_probe_matches(descriptor, path, identity, payload):
            return False
        disposition = _WindowsFileDispositionInfo(1)
        if not set_file_information(
            handle,
            4,  # FileDispositionInfo
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
    # A different object may have appeared at the original name. It is never
    # removed here, and its presence makes the capability probe fail closed.
    return not os.path.lexists(path)


def _remove_owned_probe_path_posix(
    path: Path,
    identity: _OwnedProbeIdentity,
    payload: bytes,
) -> bool:
    """Quarantine, revalidate, and remove only the owned POSIX probe file."""

    if not os.path.lexists(path):
        return True
    quarantine: Path | None = None
    for _attempt in range(32):
        candidate = path.with_name(
            f".{path.name}.cleanup-{os.getpid()}-{os.urandom(16).hex()}"
        )
        if os.path.lexists(candidate):
            continue
        try:
            _rename_no_replace_for_cleanup(path, candidate)
        except FileNotFoundError:
            return True
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno in _UNSUPPORTED_NO_REPLACE_ERRNOS:
                return _unlink_owned_probe_path_posix(path, identity, payload)
            return False
        quarantine = candidate
        break
    if quarantine is None:
        return False

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(quarantine, flags)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    try:
        if not _opened_probe_matches(
            descriptor,
            quarantine,
            identity,
            payload,
        ):
            if not os.path.lexists(path):
                try:
                    _rename_no_replace_for_cleanup(quarantine, path)
                except OSError:
                    pass
            return False
        try:
            quarantine.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
    finally:
        os.close(descriptor)
    return not os.path.lexists(path) and not os.path.lexists(quarantine)


def _unlink_owned_probe_path_posix(
    path: Path,
    identity: _OwnedProbeIdentity,
    payload: bytes,
) -> bool:
    """Remove a verified probe when the filesystem lacks exclusive rename.

    POSIX has no compare-and-unlink operation. This fallback is therefore used
    only after the filesystem itself rejects the fail-closed quarantine rename.
    It holds a no-follow descriptor, verifies the pathname and complete payload
    against the bound identity immediately before unlinking, and then verifies
    that the opened object lost its final link. A hostile same-UID actor racing
    the pathname between that comparison and unlink remains outside the local,
    cooperative-writer boundary of this capability probe.
    """

    if not os.path.lexists(path):
        return True
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        if not _opened_probe_matches(descriptor, path, identity, payload):
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        try:
            opened_after_unlink = os.fstat(descriptor)
        except OSError:
            return False
        return int(opened_after_unlink.st_nlink) == 0 and not os.path.lexists(path)
    finally:
        os.close(descriptor)


def _remove_owned_probe_path(
    path: Path,
    identity: _OwnedProbeIdentity,
    payload: bytes,
) -> bool:
    if os.name == "nt":
        return _remove_owned_probe_path_windows(path, identity, payload)
    return _remove_owned_probe_path_posix(path, identity, payload)


def probe_atomic_no_replace(directory: Path) -> Path:
    """Exercise atomic no-replace on the nearest existing destination parent.

    The probe publishes and removes a tiny owned file. It returns the exact
    directory exercised and leaves no file behind on an ordinary return.
    """

    candidate = Path(os.path.abspath(os.fspath(directory.expanduser())))
    while not os.path.lexists(candidate):
        parent = candidate.parent
        if parent == candidate:
            raise OSError(errno.ENOENT, "No destination ancestor exists.", candidate)
        candidate = parent
    metadata = candidate.lstat()
    reparse = bool(int(getattr(metadata, "st_file_attributes", 0)) & 0x400)
    if candidate.is_symlink() or reparse or not stat.S_ISDIR(metadata.st_mode):
        raise OSError(
            errno.ENOTDIR,
            "Atomic-create probe destination must be a plain directory.",
            candidate,
        )
    descriptor, source_name = tempfile.mkstemp(
        dir=candidate,
        prefix=".groove-serpent-atomic-probe-",
        suffix=".tmp",
    )
    source = Path(source_name)
    destination = source.with_name(f"{source.name}.published")
    magic = b"groove-serpent atomic no-replace probe/2\n" + os.urandom(32)
    owned_identity = _OwnedProbeIdentity.capture(os.fstat(descriptor))
    owned_payload = b""
    try:
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(magic)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            active_write_error = sys.exception()
            try:
                owned_identity, owned_payload = _refresh_written_probe(
                    source,
                    owned_identity,
                    magic,
                )
                if active_write_error is None and owned_payload != magic:
                    raise OSError(
                        errno.EIO,
                        "Atomic no-replace probe write completed with short data.",
                        source,
                    )
            except OSError as identity_error:
                if active_write_error is None:
                    raise
                active_write_error.add_note(
                    "Atomic no-replace probe could not refresh its cleanup "
                    f"identity after interrupted I/O: {identity_error}"
                )
        if os.path.lexists(destination):
            raise FileExistsError(
                errno.EEXIST,
                "Atomic-create probe destination unexpectedly exists.",
                destination,
            )
        rename_no_replace(source, destination)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            published_descriptor = os.open(destination, flags)
        except OSError as exc:
            raise OSError(
                errno.EIO,
                "Atomic no-replace probe did not preserve one exact file.",
                destination,
            ) from exc
        try:
            preserved = _opened_probe_matches(
                published_descriptor,
                destination,
                owned_identity,
                owned_payload,
            )
        finally:
            os.close(published_descriptor)
        if os.path.lexists(source) or not preserved:
            raise OSError(
                errno.EIO,
                "Atomic no-replace probe did not preserve one exact file.",
                destination,
            )
    finally:
        active_error = sys.exception()
        source_removed = _remove_owned_probe_path(
            source,
            owned_identity,
            owned_payload,
        )
        # A rename can publish successfully and then raise before returning.
        # Inspect both bound names instead of trusting a post-call flag.
        destination_removed = _remove_owned_probe_path(
            destination,
            owned_identity,
            owned_payload,
        )
        if not source_removed or not destination_removed:
            cleanup_error = OSError(
                errno.EBUSY,
                "Atomic no-replace probe cleanup lost ownership; unknown files "
                "were left untouched.",
                candidate,
            )
            if active_error is None:
                raise cleanup_error
            active_error.add_note(str(cleanup_error))
    return candidate


__all__ = ["probe_atomic_no_replace", "rename_no_replace"]
