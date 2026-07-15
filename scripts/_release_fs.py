"""Small, dependency-free filesystem primitives for release scripts."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import secrets
import shutil
import stat
import struct
import sys
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterator, NoReturn, Sequence


_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_REPARSE_POINT_ATTRIBUTE = 0x400
_CHUNK_BYTES = 1024 * 1024
_AT_EMPTY_PATH = 0x1000
_AT_SYMLINK_NOFOLLOW = 0x100
_STATX_BTIME = 0x0800
_STATX_BUFFER_BYTES = 256
_MAX_PORTABLE_RELATIVE_CODE_UNITS = 240
_MAX_PORTABLE_COMPONENT_CODE_UNITS = 255
_WINDOWS_FORBIDDEN = frozenset('<>:"\\|?*')
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
    | {f"com{index}" for index in "¹²³"}
    | {f"lpt{index}" for index in "¹²³"}
)


def canonical_portable_relative_path(value: str, context: str) -> tuple[str, str]:
    """Return one exact portable path and its NFC/casefold collision key."""

    if not value or "\\" in value or "\x00" in value:
        raise RuntimeError(f"{context} is not an exact forward-slash relative path: {value!r}")
    if value != unicodedata.normalize("NFC", value):
        raise RuntimeError(f"{context} is not canonical NFC text: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or path.as_posix() != value:
        raise RuntimeError(f"{context} is not an exact forward-slash relative path: {value!r}")
    for component in path.parts:
        if component in {"", ".", ".."}:
            raise RuntimeError(f"{context} contains traversal or an empty component: {value!r}")
        if component[-1] in {" ", "."}:
            raise RuntimeError(f"{context} has a trailing space or period: {value!r}")
        if any(
            unicodedata.category(character) == "Cc" or character in _WINDOWS_FORBIDDEN
            for character in component
        ):
            raise RuntimeError(f"{context} contains a Windows-unsafe character: {value!r}")
        if component.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
            raise RuntimeError(f"{context} uses a reserved Windows device name: {value!r}")
        try:
            component_units = len(component.encode("utf-16-le")) // 2
        except UnicodeEncodeError as exc:
            raise RuntimeError(f"{context} is not valid Unicode text: {value!r}") from exc
        if component_units > _MAX_PORTABLE_COMPONENT_CODE_UNITS:
            raise RuntimeError(f"{context} has an overlong component: {value!r}")
    try:
        path_units = len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise RuntimeError(f"{context} is not valid Unicode text: {value!r}") from exc
    if path_units > _MAX_PORTABLE_RELATIVE_CODE_UNITS:
        raise RuntimeError(f"{context} exceeds the portable relative-path limit: {value!r}")
    return value, value.casefold()


def _stat_incarnation(value: os.stat_result) -> tuple[str, int]:
    birth = getattr(value, "st_birthtime_ns", None)
    if birth is not None:
        return "stat-birthtime", int(birth)
    return "ctime-fallback", int(value.st_ctime_ns)


def _linux_statx_birthtime(
    descriptor: int,
    encoded_path: bytes,
    flags: int,
    expected: os.stat_result,
) -> int | None:
    """Read a stable Linux inode birth time without following a pathname link."""

    if not sys.platform.startswith("linux") or b"\x00" in encoded_path:
        return None
    libc: Any = ctypes.CDLL(None, use_errno=True)
    statx: Any = getattr(libc, "statx", None)
    if statx is None:
        return None
    statx.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_void_p,
    ]
    statx.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(_STATX_BUFFER_BYTES)
    if int(statx(descriptor, encoded_path, flags, _STATX_BTIME, buffer)) != 0:
        return None
    raw = buffer.raw
    mask = int(struct.unpack_from("=I", raw, 0)[0])
    inode = int(struct.unpack_from("=Q", raw, 32)[0])
    mode = int(struct.unpack_from("=H", raw, 28)[0])
    device_major = int(struct.unpack_from("=I", raw, 136)[0])
    device_minor = int(struct.unpack_from("=I", raw, 140)[0])
    if (
        not mask & _STATX_BTIME
        or inode != int(expected.st_ino)
        or stat.S_IFMT(mode) != stat.S_IFMT(expected.st_mode)
        or os.makedev(device_major, device_minor) != int(expected.st_dev)
    ):
        return None
    seconds = int(struct.unpack_from("=q", raw, 80)[0])
    nanoseconds = int(struct.unpack_from("=I", raw, 88)[0])
    if nanoseconds >= 1_000_000_000:
        return None
    return seconds * 1_000_000_000 + nanoseconds


def _path_incarnation(path: Path, value: os.stat_result) -> tuple[str, int]:
    native = getattr(value, "st_birthtime_ns", None)
    if native is not None:
        return "stat-birthtime", int(native)
    statx_birth = _linux_statx_birthtime(
        _AT_FDCWD,
        os.fsencode(path),
        _AT_SYMLINK_NOFOLLOW,
        value,
    )
    if statx_birth is not None:
        return "linux-statx-birthtime", statx_birth
    # On a filesystem without birth-time authority, ctime makes cleanup fail
    # closed after mutation or rename instead of accepting a reused inode.
    return "ctime-fallback", int(value.st_ctime_ns)


def require_stable_creation_identity(path: Path, context: str) -> None:
    """Fail before publication on a filesystem with only mutable ctime identity."""

    candidate = Path(os.path.abspath(os.fspath(path)))
    while not os.path.lexists(candidate):
        parent = candidate.parent
        if parent == candidate:
            raise RuntimeError(f"{context} has no inspectable filesystem ancestor: {path}")
        candidate = parent
    try:
        value = candidate.lstat()
    except OSError as exc:
        raise RuntimeError(f"{context} filesystem cannot be inspected: {candidate}") from exc
    if stat.S_ISLNK(value.st_mode) or is_reparse(value) or not stat.S_ISDIR(value.st_mode):
        raise RuntimeError(f"{context} filesystem ancestor is not a plain directory: {candidate}")
    source, _incarnation = _path_incarnation(candidate, value)
    if source == "ctime-fallback":
        raise RuntimeError(
            f"{context} filesystem lacks stable creation identity; publication is refused "
            "before creating outputs."
        )


def _descriptor_incarnation(
    descriptor: int,
    value: os.stat_result,
) -> tuple[str, int]:
    native = getattr(value, "st_birthtime_ns", None)
    if native is not None:
        return "stat-birthtime", int(native)
    statx_birth = _linux_statx_birthtime(
        descriptor,
        b"",
        _AT_EMPTY_PATH | _AT_SYMLINK_NOFOLLOW,
        value,
    )
    if statx_birth is not None:
        return "linux-statx-birthtime", statx_birth
    return "ctime-fallback", int(value.st_ctime_ns)


@dataclass(frozen=True, slots=True)
class PathIdentity:
    """Identity sufficient to avoid cleaning up an intervening pathname winner."""

    device: int
    inode: int
    file_type: int
    file_attributes: int
    incarnation_source: str
    incarnation_ns: int
    bound_size: int | None = None
    bound_modified_ns: int | None = None
    content_sha256: str | None = None

    @classmethod
    def capture(
        cls,
        value: os.stat_result,
        *,
        bind_file: bool = False,
        content_sha256: str | None = None,
        incarnation: tuple[str, int] | None = None,
    ) -> PathIdentity:
        if content_sha256 is not None and not bind_file:
            raise ValueError("A cleanup content digest requires a bound file identity.")
        source, incarnation_ns = _stat_incarnation(value) if incarnation is None else incarnation
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            file_type=stat.S_IFMT(value.st_mode),
            file_attributes=int(getattr(value, "st_file_attributes", 0)),
            incarnation_source=source,
            incarnation_ns=incarnation_ns,
            bound_size=int(value.st_size) if bind_file else None,
            bound_modified_ns=int(value.st_mtime_ns) if bind_file else None,
            content_sha256=content_sha256,
        )

    def matches(
        self,
        value: os.stat_result,
        *,
        incarnation: tuple[str, int] | None = None,
    ) -> bool:
        """Return whether ``value`` still identifies this captured object."""

        source, incarnation_ns = _stat_incarnation(value) if incarnation is None else incarnation
        return (
            self.device == int(value.st_dev)
            and self.inode == int(value.st_ino)
            and self.file_type == stat.S_IFMT(value.st_mode)
            and self.file_attributes == int(getattr(value, "st_file_attributes", 0))
            and self.incarnation_source == source
            and self.incarnation_ns == incarnation_ns
            and (self.bound_size is None or self.bound_size == int(value.st_size))
            and (self.bound_modified_ns is None or self.bound_modified_ns == int(value.st_mtime_ns))
        )

    def matches_path(
        self,
        path: Path,
        value: os.stat_result | None = None,
    ) -> bool:
        """Match one pathname including its stable creation incarnation."""

        current = path.lstat() if value is None else value
        return self.matches(
            current,
            incarnation=_path_incarnation(path, current),
        )

    def matches_descriptor(
        self,
        descriptor: int,
        value: os.stat_result | None = None,
    ) -> bool:
        """Match one open descriptor including its stable incarnation."""

        current = os.fstat(descriptor) if value is None else value
        return self.matches(
            current,
            incarnation=_descriptor_incarnation(descriptor, current),
        )


def is_reparse(value: os.stat_result) -> bool:
    """Return whether Windows classified an entry as a reparse point."""

    return bool(int(getattr(value, "st_file_attributes", 0)) & _REPARSE_POINT_ATTRIBUTE)


def _validate_plain_directory(
    path: Path,
    value: os.stat_result,
    context: str,
) -> None:
    if (
        stat.S_ISLNK(value.st_mode)
        or is_reparse(value)
        or not stat.S_ISDIR(value.st_mode)
        # Windows directory link counts do not include POSIX-style structural
        # ``.`` and child-directory references, so more than one is unsafe.
        or (os.name == "nt" and int(value.st_nlink) != 1)
    ):
        raise RuntimeError(f"{context} is not a plain directory: {path}")


def inspect_plain_directory(path: Path, context: str) -> os.stat_result:
    """Inspect one directory without accepting symbolic or reparse traversal."""

    try:
        value = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{context} cannot be inspected: {path}") from exc
    _validate_plain_directory(path, value, context)
    return value


def ensure_plain_ancestry(path: Path, root: Path, context: str) -> None:
    """Reject links/reparse points in every lexical parent below ``root``."""

    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"{context} escapes its root: {path}") from exc
    inspect_plain_directory(root, context)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        inspect_plain_directory(current, context)


def ensure_plain_directory_path(
    path: Path,
    context: str,
    *,
    create: bool = False,
) -> Path:
    """Return an absolute directory after auditing every lexical component.

    Missing components are created one at a time only when ``create`` is true,
    then immediately inspected.  This deliberately does not call ``resolve``:
    resolving first would erase evidence that a supplied component was a link
    or Windows reparse point.
    """

    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    anchor = Path(absolute.anchor)
    if not absolute.anchor:
        raise RuntimeError(f"{context} is not absolute: {path}")
    inspect_plain_directory(anchor, context)
    current = anchor
    for part in absolute.relative_to(anchor).parts:
        current /= part
        try:
            inspect_plain_directory(current, context)
        except RuntimeError:
            if not create or os.path.lexists(current):
                raise
            try:
                current.mkdir()
            except FileExistsError:
                pass
            except OSError as mkdir_exc:
                raise RuntimeError(f"{context} cannot be created safely: {current}") from mkdir_exc
            inspect_plain_directory(current, context)
    return absolute


def inspect_single_link_file(path: Path, context: str) -> os.stat_result:
    """Reject links, reparse points, hardlinks, and non-regular files."""

    try:
        value = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{context} cannot be inspected: {path}") from exc
    if (
        stat.S_ISLNK(value.st_mode)
        or is_reparse(value)
        or not stat.S_ISREG(value.st_mode)
        or int(value.st_nlink) != 1
    ):
        raise RuntimeError(f"{context} is not a single-link regular file: {path}")
    return value


def walk_plain_tree(
    root: Path,
    context: str,
    *,
    skip_directory: Callable[[Path], bool] | None = None,
    include_directories: bool = False,
) -> Iterator[Path]:
    """Yield files (and optionally directories) from one plain tree."""

    before = inspect_plain_directory(root, context)
    try:
        with os.scandir(root) as scanned:
            entries = sorted(scanned, key=lambda item: item.name.casefold())
    except OSError as exc:
        raise RuntimeError(f"{context} cannot be enumerated: {root}") from exc
    for entry in entries:
        path = root / entry.name
        try:
            # pathlib's Windows lstat obtains the link count through the full
            # handle-backed path query; DirEntry.stat may report ``0`` here.
            value = path.lstat()
        except OSError as exc:
            raise RuntimeError(f"{context} entry cannot be inspected: {path}") from exc
        if stat.S_ISLNK(value.st_mode) or is_reparse(value):
            raise RuntimeError(f"{context} contains a link or reparse point: {path}")
        if stat.S_ISDIR(value.st_mode):
            _validate_plain_directory(path, value, context)
            if skip_directory is None or not skip_directory(path):
                if include_directories:
                    yield path
                yield from walk_plain_tree(
                    path,
                    context,
                    skip_directory=skip_directory,
                    include_directories=include_directories,
                )
        elif stat.S_ISREG(value.st_mode):
            if int(value.st_nlink) != 1:
                raise RuntimeError(f"{context} contains a multi-link file: {path}")
            yield path
        else:
            raise RuntimeError(f"{context} contains a non-regular entry: {path}")
    try:
        after = root.lstat()
    except OSError as exc:
        raise RuntimeError(f"{context} changed during enumeration: {root}") from exc
    _validate_plain_directory(root, after, context)
    if not os.path.samestat(before, after):
        raise RuntimeError(f"{context} changed identity during enumeration: {root}")


def read_single_link_file(path: Path, maximum: int, context: str) -> bytes:
    """Read one bounded file from the same object that passed the link audit."""

    before = inspect_single_link_file(path, context)
    if int(before.st_size) < 0 or int(before.st_size) > maximum:
        raise RuntimeError(f"{context} exceeds {maximum} bytes: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"{context} cannot be opened safely: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or is_reparse(opened)
            or int(opened.st_nlink) != 1
            or not os.path.samestat(before, opened)
        ):
            raise RuntimeError(f"{context} changed identity while opening: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(_CHUNK_BYTES, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise RuntimeError(f"{context} exceeded {maximum} bytes: {path}")
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)

    try:
        after = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{context} changed after reading: {path}") from exc
    if (
        stat.S_ISLNK(after.st_mode)
        or is_reparse(after)
        or not stat.S_ISREG(after.st_mode)
        or int(after.st_nlink) != 1
        or not os.path.samestat(before, after)
        or int(after.st_size) != len(payload)
    ):
        raise RuntimeError(f"{context} changed while reading: {path}")
    return payload


def zip_layout_is_exact(
    stream: BinaryIO,
    size: int,
    infos: Sequence[zipfile.ZipInfo],
) -> bool:
    """Require the complete normalized ZIP profile, with no unbound bytes."""

    if size < 22 or not infos:
        return False
    try:
        original_position = stream.tell()
    except (OSError, ValueError):
        return False

    def read_at(offset: int, count: int) -> bytes:
        if offset < 0 or count < 0 or offset + count > size:
            return b""
        stream.seek(offset)
        return stream.read(count)

    try:
        eocd_offset = size - 22
        eocd = read_at(eocd_offset, 22)
        if len(eocd) != 22:
            return False
        (
            signature,
            disk_number,
            central_disk,
            entries_on_disk,
            entries_total,
            central_size,
            central_offset,
            comment_size,
        ) = struct.unpack("<4s4H2LH", eocd)
        if (
            signature != b"PK\x05\x06"
            or disk_number != 0
            or central_disk != 0
            or entries_on_disk != len(infos)
            or entries_total != len(infos)
            or comment_size != 0
            or central_offset + central_size != eocd_offset
        ):
            return False

        expected_local_offset = 0
        central_cursor = central_offset
        for info in infos:
            if (
                info.header_offset != expected_local_offset
                or info.is_dir()
                or info.date_time != (1980, 1, 1, 0, 0, 0)
                or info.create_system != 3
                or info.create_version != 20
                or info.extract_version != 20
                or info.reserved != 0
                or info.volume != 0
                or info.internal_attr != 0
                or info.extra != b""
                or info.comment != b""
                or info.flag_bits not in {0, 0x0800}
                or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            ):
                return False
            local = read_at(expected_local_offset, 30)
            if len(local) != 30:
                return False
            (
                local_signature,
                local_extract_version,
                flags,
                compression,
                modified_time,
                modified_date,
                crc,
                compressed_size,
                uncompressed_size,
                name_size,
                extra_size,
            ) = struct.unpack("<4s5H3L2H", local)
            try:
                encoded_name = info.filename.encode("ascii")
                expected_flags = 0
            except UnicodeEncodeError:
                encoded_name = info.filename.encode("utf-8")
                expected_flags = 0x0800
            name = read_at(expected_local_offset + 30, name_size)
            extra = read_at(expected_local_offset + 30 + name_size, extra_size)
            if (
                local_signature != b"PK\x03\x04"
                or local_extract_version != info.extract_version
                or flags != info.flag_bits
                or flags != expected_flags
                or flags & 0x0008
                or compression != info.compress_type
                or modified_time != 0
                or modified_date != 0x0021
                or crc != info.CRC
                or compressed_size != info.compress_size
                or uncompressed_size != info.file_size
                or name != encoded_name
                or extra != b""
            ):
                return False
            expected_local_offset += 30 + name_size + extra_size + compressed_size

            central = read_at(central_cursor, 46)
            if len(central) != 46:
                return False
            (
                central_signature,
                version_made,
                central_extract_version,
                central_flags,
                central_compression,
                central_modified_time,
                central_modified_date,
                central_crc,
                central_compressed_size,
                central_uncompressed_size,
                central_name_size,
                central_extra_size,
                central_comment_size,
                disk_start,
                internal_attr,
                external_attr,
                local_header_offset,
            ) = struct.unpack("<4s6H3L5H2L", central)
            central_name = read_at(central_cursor + 46, central_name_size)
            central_extra = read_at(
                central_cursor + 46 + central_name_size,
                central_extra_size,
            )
            central_comment = read_at(
                central_cursor + 46 + central_name_size + central_extra_size,
                central_comment_size,
            )
            if (
                central_signature != b"PK\x01\x02"
                or version_made != info.create_version | (info.create_system << 8)
                or central_extract_version != info.extract_version
                or central_flags != info.flag_bits
                or central_compression != info.compress_type
                or central_modified_time != 0
                or central_modified_date != 0x0021
                or central_crc != info.CRC
                or central_compressed_size != info.compress_size
                or central_uncompressed_size != info.file_size
                or central_name != encoded_name
                or central_extra != info.extra
                or central_comment != info.comment
                or disk_start != info.volume
                or internal_attr != info.internal_attr
                or external_attr != info.external_attr
                or local_header_offset != info.header_offset
            ):
                return False
            central_cursor += 46 + central_name_size + central_extra_size + central_comment_size
        return bool(
            expected_local_offset == central_offset
            and central_cursor == central_offset + central_size
        )
    except (OSError, UnicodeEncodeError, ValueError, struct.error):
        return False
    finally:
        try:
            stream.seek(original_position)
        except (OSError, ValueError):
            pass


def capture_identity(
    path: Path,
    *,
    bind_file: bool = False,
    content_sha256: str | None = None,
) -> PathIdentity:
    """Capture the current pathname object for conservative later cleanup."""

    value = path.lstat()
    return PathIdentity.capture(
        value,
        bind_file=bind_file,
        content_sha256=content_sha256,
        incarnation=_path_incarnation(path, value),
    )


def capture_descriptor_identity(
    descriptor: int,
    *,
    bind_file: bool = False,
    content_sha256: str | None = None,
) -> PathIdentity:
    """Capture one open descriptor with the strongest host incarnation token."""

    value = os.fstat(descriptor)
    return PathIdentity.capture(
        value,
        bind_file=bind_file,
        content_sha256=content_sha256,
        incarnation=_descriptor_incarnation(descriptor, value),
    )


def _restore_quarantine(quarantine: Path, original: Path) -> None:
    """Best-effort restoration used only when a quarantined object was not ours."""

    if os.path.lexists(original):
        return
    try:
        rename_no_replace(quarantine, original)
    except OSError:
        # Preserving an unexpected object in quarantine is safer than deleting
        # it or replacing a new pathname winner.
        return


def _quarantine_owned_path(
    path: Path,
    identity: PathIdentity,
) -> tuple[Path | None, bool]:
    """Atomically move an owned pathname aside without replacing any entry.

    The unpredictable same-directory name closes the checked-name cleanup
    race.  If the object moved was not the captured object, it is restored when
    possible and is never deleted by this helper.
    """

    for _attempt in range(32):
        quarantine = path.with_name(f".{path.name}.cleanup-{os.getpid()}-{secrets.token_hex(16)}")
        if os.path.lexists(quarantine):
            continue
        try:
            rename_no_replace(path, quarantine)
        except FileNotFoundError:
            return None, True
        except FileExistsError:
            continue
        except OSError:
            return None, False
        try:
            moved = quarantine.lstat()
        except OSError:
            return None, False
        if not identity.matches_path(quarantine, moved):
            _restore_quarantine(quarantine, path)
            return None, False
        return quarantine, False
    return None, False


def unlink_if_owned_file(path: Path, identity: PathIdentity) -> bool:
    """Quarantine and remove only the captured single-link regular file."""

    quarantine, safely_absent = _quarantine_owned_path(path, identity)
    if quarantine is None:
        return safely_absent
    try:
        current = inspect_single_link_file(quarantine, "Owned cleanup file")
    except RuntimeError:
        _restore_quarantine(quarantine, path)
        return False
    if not identity.matches_path(quarantine, current):
        _restore_quarantine(quarantine, path)
        return False
    if identity.content_sha256 is not None:
        if identity.bound_size is None:
            _restore_quarantine(quarantine, path)
            return False
        try:
            payload = read_single_link_file(
                quarantine,
                identity.bound_size,
                "Owned cleanup file",
            )
        except RuntimeError:
            _restore_quarantine(quarantine, path)
            return False
        if (
            len(payload) != identity.bound_size
            or hashlib.sha256(payload).hexdigest() != identity.content_sha256
        ):
            _restore_quarantine(quarantine, path)
            return False
    try:
        quarantine.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def unlink_owned_file_candidates(
    paths: Sequence[Path],
    identity: PathIdentity,
) -> bool:
    """Remove an owned file from every possible publication pathname.

    A no-replace rename can complete immediately before an asynchronous
    exception is delivered.  Callers therefore cannot safely choose between
    the staging and destination names using a flag set after the rename.  This
    helper inspects both names without following links and removes only the
    object bound by ``identity``.  An unrelated concurrent winner is retained.
    """

    clean = True
    saw_existing = False
    saw_owned = False
    for path in dict.fromkeys(paths):
        try:
            current = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            clean = False
            continue
        saw_existing = True
        if not identity.matches_path(path, current):
            continue
        saw_owned = True
        if not unlink_if_owned_file(path, identity):
            clean = False

    for path in dict.fromkeys(paths):
        try:
            current = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            clean = False
            continue
        if identity.matches_path(path, current):
            clean = False

    # If a candidate exists but neither candidate ever held our object, the
    # object may have been displaced outside the bounded cleanup set.  Report
    # lost ownership while preserving every unrelated pathname.
    if saw_existing and not saw_owned:
        clean = False
    return clean


def remove_owned_tree(path: Path, identity: PathIdentity) -> bool:
    """Quarantine and recursively remove only the captured plain directory."""

    quarantine, safely_absent = _quarantine_owned_path(path, identity)
    if quarantine is None:
        return safely_absent
    try:
        current = inspect_plain_directory(quarantine, "Owned cleanup directory")
    except RuntimeError:
        _restore_quarantine(quarantine, path)
        return False
    if not identity.matches_path(quarantine, current):
        _restore_quarantine(quarantine, path)
        return False
    try:
        for _member in walk_plain_tree(
            quarantine,
            "Owned cleanup directory tree",
        ):
            pass
    except RuntimeError:
        _restore_quarantine(quarantine, path)
        return False
    try:
        shutil.rmtree(quarantine)
    except OSError:
        return False
    return True


def remove_owned_tree_candidates(
    paths: Sequence[Path],
    identity: PathIdentity,
) -> bool:
    """Remove an owned tree from all possible publication pathnames."""

    clean = True
    saw_existing = False
    saw_owned = False
    for path in dict.fromkeys(paths):
        try:
            current = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            clean = False
            continue
        saw_existing = True
        if not identity.matches_path(path, current):
            continue
        saw_owned = True
        if not remove_owned_tree(path, identity):
            clean = False

    for path in dict.fromkeys(paths):
        try:
            current = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            clean = False
            continue
        if identity.matches_path(path, current):
            clean = False

    if saw_existing and not saw_owned:
        clean = False
    return clean


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
        raise OSError(errno.ENOTSUP, "renameat2 is unavailable", destination)
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
        raise OSError(errno.ENOTSUP, "atomic no-replace is unavailable", destination)
    _raise_rename_error(error, destination)


def _darwin_rename_no_replace(source: Path, destination: Path) -> None:
    libc: Any = ctypes.CDLL(None, use_errno=True)
    renamex_np = getattr(libc, "renamex_np", None)
    if renamex_np is None:
        raise OSError(errno.ENOTSUP, "renamex_np is unavailable", destination)
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
        raise OSError(errno.ENOTSUP, "atomic no-replace is unavailable", destination)
    _raise_rename_error(error, destination)


def rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename within one directory without replacing a winner."""

    if source.parent != destination.parent:
        raise ValueError("Atomic no-replace rename requires one parent directory.")
    if os.name == "nt":
        os.rename(source, destination)
        return
    if sys.platform.startswith("linux"):
        _linux_rename_no_replace(source, destination)
        return
    if sys.platform == "darwin":
        _darwin_rename_no_replace(source, destination)
        return
    raise OSError(errno.ENOTSUP, "atomic no-replace is unsupported", destination)


__all__ = [
    "PathIdentity",
    "canonical_portable_relative_path",
    "capture_descriptor_identity",
    "capture_identity",
    "ensure_plain_ancestry",
    "ensure_plain_directory_path",
    "inspect_plain_directory",
    "inspect_single_link_file",
    "is_reparse",
    "read_single_link_file",
    "require_stable_creation_identity",
    "remove_owned_tree",
    "remove_owned_tree_candidates",
    "rename_no_replace",
    "unlink_if_owned_file",
    "unlink_owned_file_candidates",
    "walk_plain_tree",
    "zip_layout_is_exact",
]
