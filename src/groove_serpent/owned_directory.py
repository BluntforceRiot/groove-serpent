"""Bounded cleanup of transaction directories owned by local cooperative writers.

Receipts bind the original root and explicitly registered directories, not every
file payload. Unknown directories, links, and special files are preserved. Root
quarantine narrows pathname races; this is not a hostile same-UID filesystem or
an absolute POSIX compare-and-delete guarantee.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .atomic_create import rename_no_replace
from .file_identity import stable_creation_time_ns

_REPARSE_POINT = 0x400
_MAX_ENTRIES = 100_000
_MAX_DEPTH = 64


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    device: int
    inode: int
    birth_ns: int | None


@dataclass(slots=True)
class OwnedDirectoryReceipt:
    """In-process ownership captured during creation, never during failure cleanup."""

    path: Path
    parent_identity: _DirectoryIdentity
    directories: dict[Path, _DirectoryIdentity] = field(default_factory=dict)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _refuse(path: Path, reason: str) -> None:
    raise OSError(errno.EBUSY, f"Owned directory cleanup preserved {reason}", str(path))


def _identity(path: Path) -> _DirectoryIdentity:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_POINT
        or int(metadata.st_ino) <= 0
    ):
        _refuse(path, "a non-directory, linked, or unverifiable entry")
    return _DirectoryIdentity(
        int(metadata.st_dev), int(metadata.st_ino), stable_creation_time_ns(metadata),
    )


def _plain_ancestors(path: Path) -> None:
    for ancestor in (path, *path.parents):
        _identity(ancestor)


def capture_owned_directory_receipt(path: Path) -> OwnedDirectoryReceipt:
    """Capture an empty root immediately after this operation creates it."""

    path = _absolute(path)
    _plain_ancestors(path.parent)
    parent = _identity(path.parent)
    identity = _identity(path)
    if any(path.iterdir()):
        _refuse(path, "a root that was not empty at ownership capture")
    if _identity(path) != identity or _identity(path.parent) != parent:
        _refuse(path, "a root or parent changed during ownership capture")
    return OwnedDirectoryReceipt(path, parent, {Path("."): identity})


def _relative(path: Path, receipt: OwnedDirectoryReceipt) -> Path:
    try:
        return _absolute(path).relative_to(receipt.path)
    except ValueError as exc:
        raise OSError(errno.EBUSY, "Cleanup path is outside its owned root", str(path)) from exc


def _assert_parent(path: Path, receipt: OwnedDirectoryReceipt, relative: Path) -> None:
    _plain_ancestors(path.parent)
    expected = (
        receipt.parent_identity if relative == Path(".")
        else receipt.directories.get(relative.parent)
    )
    if expected is None or _identity(path.parent) != expected:
        _refuse(path, "a substituted or unregistered parent")
    if relative != Path("."):
        if _identity(receipt.path) != receipt.directories[Path(".")]:
            _refuse(receipt.path, "a substituted root")


def register_owned_directory(receipt: OwnedDirectoryReceipt, path: Path) -> None:
    """Register a child when created or returned by a successful owned operation.

    Registration never refreshes an earlier identity. Do not call this to adopt
    entries discovered after an operation fails.
    """

    path = _absolute(path)
    relative = _relative(path, receipt)
    _assert_parent(path, receipt, relative)
    identity = _identity(path)
    existing = receipt.directories.get(relative)
    if existing is not None and existing != identity:
        _refuse(path, "a replaced registered directory")
    receipt.directories[relative] = identity


def _validate_tree(path: Path, receipt: OwnedDirectoryReceipt, relative: Path) -> None:
    pending = [(path, relative, 0)]
    count = 0
    while pending:
        directory, key, depth = pending.pop()
        expected = receipt.directories.get(key)
        if expected is None or _identity(directory) != expected:
            _refuse(directory, "a substituted or unregistered directory")
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                child = directory / entry.name
                child_key = key / entry.name
                if count > _MAX_ENTRIES:
                    _refuse(path, "an oversized tree")
                metadata = child.lstat()
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_POINT
                ):
                    _refuse(child, "a linked or reparse entry")
                if stat.S_ISDIR(metadata.st_mode):
                    if depth >= _MAX_DEPTH:
                        _refuse(child, "an excessively nested tree")
                    pending.append((child, child_key, depth + 1))
                elif not stat.S_ISREG(metadata.st_mode) or child_key in receipt.directories:
                    _refuse(child, "a special file or replaced directory")


def assert_owned_directory_receipt(path: Path, receipt: OwnedDirectoryReceipt) -> None:
    """Revalidate root/parent, known directory identities, and plain tree entries."""

    path = _absolute(path)
    relative = _relative(path, receipt)
    _assert_parent(path, receipt, relative)
    _validate_tree(path, receipt, relative)


def remove_owned_directory_if_present(path: Path, receipt: OwnedDirectoryReceipt) -> None:
    """Quarantine and remove an owned root/subtree; refuse uncertain ownership.

    A refused quarantine is restored only to a vacant original name. If that is
    impossible, its preserved quarantine path is reported in the raised error.
    """

    path = _absolute(path)
    relative = _relative(path, receipt)
    _assert_parent(path, receipt, relative)
    if not os.path.lexists(path):
        return
    _validate_tree(path, receipt, relative)
    quarantine = path.with_name(f".owned-cleanup-{uuid.uuid4().hex}.partial")
    if os.path.lexists(quarantine):
        _refuse(quarantine, "a preexisting quarantine occupant")
    renamed = False
    try:
        rename_no_replace(path, quarantine)
        renamed = True
        _assert_parent(quarantine, receipt, relative)
        _validate_tree(quarantine, receipt, relative)
        if _identity(quarantine) != receipt.directories.get(relative):
            _refuse(quarantine, "a root changed during quarantine validation")
        shutil.rmtree(quarantine)
    except BaseException as exc:
        restored = False
        if not os.path.lexists(path) and os.path.lexists(quarantine):
            try:
                if renamed or _identity(quarantine) == receipt.directories.get(relative):
                    rename_no_replace(quarantine, path)
                    restored = True
            except OSError:
                pass
        preserved = path if restored or os.path.lexists(path) else quarantine
        exc.add_note(f"Directory cleanup preserved material if present at {preserved}.")
        raise
    if os.path.lexists(path):
        _refuse(path, "a new occupant at the vacated original name")


__all__ = [
    "OwnedDirectoryReceipt",
    "assert_owned_directory_receipt",
    "capture_owned_directory_receipt",
    "register_owned_directory",
    "remove_owned_directory_if_present",
]
