from __future__ import annotations

import ctypes
import hashlib
import importlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ExportError


PUBLICATION_MANIFEST_SCHEMA = "groove-serpent.publication-manifest/2"


@dataclass(frozen=True, slots=True)
class FileReceipt:
    """A content hash plus the strongest portable local file identity available."""

    sha256: str
    size_bytes: int
    modified_ns: int
    status_changed_ns: int
    device: int
    inode: int
    mode: int
    birth_ns: int | None
    file_attributes: int | None

    @classmethod
    def from_stat(cls, stat: os.stat_result, sha256: str) -> "FileReceipt":
        birth_ns = getattr(stat, "st_birthtime_ns", None)
        file_attributes = getattr(stat, "st_file_attributes", None)
        return cls(
            sha256=sha256,
            size_bytes=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            status_changed_ns=stat.st_ctime_ns,
            device=stat.st_dev,
            inode=stat.st_ino,
            mode=stat.st_mode,
            birth_ns=int(birth_ns) if birth_ns is not None else None,
            file_attributes=(
                int(file_attributes) if file_attributes is not None else None
            ),
        )

    def identity_dict(self) -> dict[str, int | None]:
        values = asdict(self)
        del values["sha256"]
        return values

    def same_file_object(self, other: "FileReceipt") -> bool:
        """Compare handle/path identity across the platform's stat APIs.

        CPython 3.13 on Windows can report the same NTFS file's ``st_ctime_ns``
        with slightly different precision through ``fstat`` and ``stat``.  Change
        time remains part of every captured receipt and is compared between stable
        handles/operations, but it cannot be used to prove that an open handle and
        its path refer to the same file object.  Windows also synthesizes different
        permission bits for some executable files through ``fstat`` and ``stat``;
        retain the file-type bits there while exact receipts still preserve and
        compare the original modes across like-for-like observations.
        """

        self_mode = stat.S_IFMT(self.mode) if os.name == "nt" else self.mode
        other_mode = stat.S_IFMT(other.mode) if os.name == "nt" else other.mode

        return (
            self.size_bytes,
            self.modified_ns,
            self.device,
            self.inode,
            self_mode,
            self.birth_ns,
            self.file_attributes,
        ) == (
            other.size_bytes,
            other.modified_ns,
            other.device,
            other.inode,
            other_mode,
            other.birth_ns,
            other.file_attributes,
        )

    def manifest_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "file_identity": self.identity_dict(),
        }


@dataclass(frozen=True, slots=True)
class PathReceipt:
    """An exact path-stat observation plus the platform change-time identity."""

    file_receipt: FileReceipt
    platform_status_changed_ns: int


@dataclass(frozen=True, slots=True)
class VerifiedCopyCapture:
    """One-pass copy receipts for both open handles and final path stats.

    Handle receipts retain the established full-hash verification behavior.
    Path receipts preserve an exact same-API observation, including change
    time, for cheap repeated identity checks without rereading file contents.
    """

    source_receipt: FileReceipt
    snapshot_receipt: FileReceipt
    source_path_receipt: PathReceipt
    snapshot_path_receipt: PathReceipt


class _WindowsFileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("creation_time", ctypes.c_int64),
        ("last_access_time", ctypes.c_int64),
        ("last_write_time", ctypes.c_int64),
        ("change_time", ctypes.c_int64),
        ("file_attributes", ctypes.c_uint32),
    ]


def _platform_status_changed_ns(descriptor: int, value: os.stat_result) -> int:
    """Return actual status/change time, including NTFS ChangeTime on Windows."""

    if os.name != "nt":
        return int(value.st_ctime_ns)
    loader: Any = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise OSError("Windows change-time inspection is unavailable")
    msvcrt: Any = importlib.import_module("msvcrt")
    kernel32: Any = loader("kernel32", use_last_error=True)
    kernel32.GetFileInformationByHandleEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.GetFileInformationByHandleEx.restype = ctypes.c_int
    handle = int(msvcrt.get_osfhandle(descriptor))
    info = _WindowsFileBasicInfo()
    succeeded = kernel32.GetFileInformationByHandleEx(
        ctypes.c_void_p(handle),
        0,  # FileBasicInfo
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not succeeded:
        get_last_error: Any = getattr(ctypes, "get_last_error", None)
        error = int(get_last_error()) if get_last_error is not None else 0
        raise OSError(error, "Windows file change time could not be inspected")
    # Windows LARGE_INTEGER timestamps use 100-nanosecond intervals.  The epoch
    # is irrelevant because receipts compare identity rather than wall time.
    return int(info.change_time) * 100


def _capture_path_receipt(path: Path, sha256: str, *, label: str) -> PathReceipt:
    """Capture one stable, no-content-read path identity receipt."""

    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        opened = os.fstat(descriptor)
        status_changed_ns = _platform_status_changed_ns(descriptor, opened)
        path_after = path.stat()
    except OSError as exc:
        raise ExportError(f"{label} could not be checked: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    opened_receipt = FileReceipt.from_stat(opened, sha256)
    path_receipt = FileReceipt.from_stat(path_after, sha256)
    if not opened_receipt.same_file_object(path_receipt):
        raise ExportError(f"{label} changed while its path receipt was captured.")
    return PathReceipt(path_receipt, status_changed_ns)


def same_file_object_stats(left: os.stat_result, right: os.stat_result) -> bool:
    """Return whether handle/path stats identify one stable file object."""

    return FileReceipt.from_stat(left, "").same_file_object(
        FileReceipt.from_stat(right, "")
    )


def canonical_json_sha256(value: Mapping[str, Any] | list[Any]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def capture_file_receipt(path: Path, *, label: str) -> FileReceipt:
    """Hash one stable opened file and prove the path still names that file."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not os.path.isfile(path):
                raise ExportError(f"{label} is not a regular file.")
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        path_after = path.stat()
    except ExportError:
        raise
    except OSError as exc:
        raise ExportError(f"{label} could not be read for publication: {exc}") from exc

    before_receipt = FileReceipt.from_stat(before, digest.hexdigest())
    after_receipt = FileReceipt.from_stat(after, digest.hexdigest())
    path_receipt = FileReceipt.from_stat(path_after, digest.hexdigest())
    if before_receipt != after_receipt or not after_receipt.same_file_object(
        path_receipt
    ):
        raise ExportError(
            f"{label} changed while its publication identity was being captured."
        )
    return before_receipt


def assert_file_receipt(path: Path, expected: FileReceipt, *, label: str) -> None:
    current = capture_file_receipt(path, label=label)
    if current != expected:
        differences = [
            key
            for key, value in asdict(expected).items()
            if asdict(current)[key] != value
        ]
        raise ExportError(
            f"{label} changed during export ({', '.join(differences)}); "
            "the staged batch was not published."
        )


def assert_file_identity(path: Path, expected: FileReceipt, *, label: str) -> None:
    """Check a path's stable identity and metadata without reading its contents."""

    try:
        current = FileReceipt.from_stat(path.stat(), expected.sha256)
    except OSError as exc:
        raise ExportError(f"{label} could not be checked: {exc}") from exc
    if not expected.same_file_object(current):
        raise ExportError(f"{label} changed after its identity was captured.")


def assert_path_receipt(path: Path, expected: PathReceipt, *, label: str) -> None:
    """Compare one exact path receipt without reading file contents.

    Unlike :func:`assert_file_identity`, both observations use the same path
    capture routine and retain native platform change time.  That avoids the
    Windows fstat/stat precision mismatch while detecting same-size rewrites
    whose ordinary modification time was restored.
    """

    current = _capture_path_receipt(
        path,
        expected.file_receipt.sha256,
        label=label,
    )
    if current != expected:
        raise ExportError(f"{label} changed after its path receipt was captured.")


def stage_verified_copy(
    source: Path,
    destination: Path,
    expected: FileReceipt,
    *,
    label: str,
) -> FileReceipt:
    """Copy one already-verified file through a stable open handle."""

    digest = hashlib.sha256()
    try:
        with source.open("rb") as source_handle:
            opened = FileReceipt.from_stat(
                os.fstat(source_handle.fileno()), expected.sha256
            )
            if opened != expected:
                raise ExportError(
                    f"{label} changed before its immutable operation snapshot was opened."
                )
            with destination.open("xb") as destination_handle:
                for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    destination_handle.write(chunk)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
            closed = FileReceipt.from_stat(
                os.fstat(source_handle.fileno()), expected.sha256
            )
        path_after = FileReceipt.from_stat(source.stat(), expected.sha256)
    except ExportError:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise ExportError(f"{label} could not be staged safely: {exc}") from exc

    if (
        opened != closed
        or not closed.same_file_object(path_after)
        or digest.hexdigest() != expected.sha256
    ):
        destination.unlink(missing_ok=True)
        raise ExportError(
            f"{label} changed while its immutable operation snapshot was being created."
        )
    snapshot = capture_file_receipt(destination, label=f"Staged {label.lower()}")
    if snapshot.sha256 != expected.sha256 or snapshot.size_bytes != expected.size_bytes:
        destination.unlink(missing_ok=True)
        raise ExportError(f"The staged {label.lower()} snapshot failed verification.")
    return snapshot


def capture_verified_copy(
    source: Path,
    destination: Path,
    *,
    label: str,
    expected_sha256: str | None = None,
    expected_size_bytes: int | None = None,
) -> VerifiedCopyCapture:
    """Copy and authenticate a stable source in one streaming read.

    The digest is calculated over the bytes written to the new, exclusively
    created destination.  Stable open-handle and path identity checks bind that
    digest to both file objects without rereading either complete file.  This is
    the capture primitive for immutable session snapshots; publication paths
    that copy an *existing* receipt continue to use :func:`stage_verified_copy`.
    """

    normalized_expected = None
    if expected_sha256 is not None:
        normalized_expected = expected_sha256.strip().lower()
        if len(normalized_expected) != 64 or any(
            character not in "0123456789abcdef"
            for character in normalized_expected
        ):
            raise ExportError(f"{label} has an invalid expected SHA-256.")
    if expected_size_bytes is not None and expected_size_bytes < 0:
        raise ExportError(f"{label} has an invalid expected byte length.")

    digest = hashlib.sha256()
    destination_created = False
    try:
        with source.open("rb") as source_handle:
            source_before = os.fstat(source_handle.fileno())
            if not stat.S_ISREG(source_before.st_mode):
                raise ExportError(f"{label} is not a regular file.")
            if (
                expected_size_bytes is not None
                and source_before.st_size != expected_size_bytes
            ):
                raise ExportError(
                    f"{label} no longer matches its expected byte length."
                )
            with destination.open("xb") as destination_handle:
                destination_created = True
                for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    written = destination_handle.write(chunk)
                    if written != len(chunk):
                        raise OSError("short write while creating immutable snapshot")
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
                destination_after = os.fstat(destination_handle.fileno())
            source_after = os.fstat(source_handle.fileno())
        sha256 = digest.hexdigest()
        source_path_receipt = _capture_path_receipt(
            source,
            sha256,
            label=label,
        )
        snapshot_path_receipt = _capture_path_receipt(
            destination,
            sha256,
            label=f"Staged {label.lower()} snapshot",
        )
    except ExportError:
        if destination_created:
            destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        if destination_created:
            destination.unlink(missing_ok=True)
        raise ExportError(
            f"{label} could not be captured as an immutable copy: {exc}"
        ) from exc

    source_receipt = FileReceipt.from_stat(source_before, sha256)
    source_closed_receipt = FileReceipt.from_stat(source_after, sha256)
    snapshot_receipt = FileReceipt.from_stat(destination_after, sha256)
    if (
        source_receipt != source_closed_receipt
        or not source_closed_receipt.same_file_object(
            source_path_receipt.file_receipt
        )
        or not snapshot_receipt.same_file_object(
            snapshot_path_receipt.file_receipt
        )
        or snapshot_receipt.size_bytes != source_receipt.size_bytes
    ):
        destination.unlink(missing_ok=True)
        raise ExportError(
            f"{label} changed while its immutable operation snapshot was being created."
        )
    if normalized_expected is not None and sha256 != normalized_expected:
        destination.unlink(missing_ok=True)
        raise ExportError(f"{label} no longer matches its expected SHA-256 identity.")
    if (
        expected_size_bytes is not None
        and source_receipt.size_bytes != expected_size_bytes
    ):
        destination.unlink(missing_ok=True)
        raise ExportError(f"{label} no longer matches its expected byte length.")
    return VerifiedCopyCapture(
        source_receipt=source_receipt,
        snapshot_receipt=snapshot_receipt,
        source_path_receipt=source_path_receipt,
        snapshot_path_receipt=snapshot_path_receipt,
    )
