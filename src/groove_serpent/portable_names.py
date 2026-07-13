"""Portable Unicode normalization for names that reach a filesystem."""

from __future__ import annotations

import os
import stat
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path


def normalize_portable_name(value: str) -> str:
    """Return the canonical NFC spelling used for generated filenames."""

    return unicodedata.normalize("NFC", value)


def portable_name_key(value: str) -> str:
    """Return one case-insensitive, normalization-insensitive filename key."""

    return normalize_portable_name(value).casefold()


def portable_relative_path_key(value: str) -> str:
    """Return a portable key for a serialized relative filesystem path."""

    return portable_name_key(value.replace("\\", "/"))


class PortablePathError(ValueError):
    """Raised when a path cannot be resolved safely on portable filesystems."""


@dataclass(frozen=True, slots=True)
class PortablePathResolution:
    """A component-wise path resolution and the state of its final entry."""

    path: Path
    entry_exists: bool


@dataclass(frozen=True, slots=True)
class _DirectoryCacheEntry:
    identity: tuple[int, ...]
    names: tuple[str, ...]


_DIRECTORY_CACHE_LIMIT = 256
_DIRECTORY_CACHE_LOCK = threading.Lock()
_DIRECTORY_CACHE: OrderedDict[str, _DirectoryCacheEntry] = OrderedDict()


def _optional_stat_int(value: object) -> int:
    return value if isinstance(value, int) else -1


def _directory_identity(parent: Path) -> tuple[int, ...]:
    try:
        details = parent.stat()
    except OSError as exc:
        raise PortablePathError(
            f"Portable path ancestor could not be inspected: {parent}"
        ) from exc
    if not stat.S_ISDIR(details.st_mode):
        raise PortablePathError(
            f"Portable path ancestor is not a directory: {parent}"
        )
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
        _optional_stat_int(getattr(details, "st_birthtime_ns", None)),
        _optional_stat_int(getattr(details, "st_file_attributes", None)),
    )


def _cached_directory_names(parent: Path) -> tuple[str, ...]:
    """Return one stable, bounded, stat-invalidated directory snapshot."""

    cache_key = os.fspath(parent)
    last_names: tuple[str, ...] | None = None
    for _attempt in range(3):
        with _DIRECTORY_CACHE_LOCK:
            before = _directory_identity(parent)
            cached = _DIRECTORY_CACHE.get(cache_key)
            if cached is not None and cached.identity == before:
                _DIRECTORY_CACHE.move_to_end(cache_key)
                return cached.names
            if cached is not None:
                del _DIRECTORY_CACHE[cache_key]

            try:
                names = tuple(entry.name for entry in parent.iterdir())
            except OSError as exc:
                raise PortablePathError(
                    f"Portable path ancestor could not be inspected: {parent}"
                ) from exc
            last_names = names
            after = _directory_identity(parent)
            if after != before:
                continue

            _DIRECTORY_CACHE[cache_key] = _DirectoryCacheEntry(after, names)
            _DIRECTORY_CACHE.move_to_end(cache_key)
            while len(_DIRECTORY_CACHE) > _DIRECTORY_CACHE_LIMIT:
                _DIRECTORY_CACHE.popitem(last=False)
            return names
    # A busy shared directory (notably the Windows temporary root) may change
    # continuously for unrelated processes. Preserve the pre-cache behavior by
    # using the latest live enumeration once, but never cache an unstable view.
    if last_names is not None:
        return last_names
    raise PortablePathError(f"Portable path ancestor could not be read: {parent}")


def _clear_directory_cache() -> None:
    """Clear portable-name observations (used by deterministic tests)."""

    with _DIRECTORY_CACHE_LOCK:
        _DIRECTORY_CACHE.clear()


def _absolute_path(path: Path) -> Path:
    """Make ``path`` absolute without resolving away portable-equivalent names."""

    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _portable_matches(parent: Path, name: str) -> list[Path]:
    target_key = portable_name_key(name)
    return [
        parent / entry_name
        for entry_name in _cached_directory_names(parent)
        if portable_name_key(entry_name) == target_key
    ]


def _unique_portable_match(parent: Path, name: str) -> Path | None:
    matches = _portable_matches(parent, name)
    if len(matches) > 1:
        spellings = ", ".join(repr(entry.name) for entry in matches)
        raise PortablePathError(
            f"Portable path ancestor is ambiguous in {parent}: {spellings}"
        )
    if matches:
        return matches[0]

    # Windows accepts an existing DOS 8.3 alias that is not returned by
    # iterdir(). Treat that OS-resolvable spelling as one exact match so a
    # short TEMP/TMP prefix does not look like a missing ancestor.
    exact = parent / name
    return exact if os.path.lexists(exact) else None


def _is_redirecting_ancestor(path: Path) -> bool:
    """Reject symlink/junction/reparse ancestors before publication writes."""

    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError as exc:
        raise PortablePathError(
            f"Portable path ancestor could not be inspected: {path}"
        ) from exc
    # FILE_ATTRIBUTE_REPARSE_POINT. This includes Windows junctions and keeps
    # publication from being silently redirected outside the requested tree.
    return bool(attributes & 0x0400)


def resolve_portable_path(
    path: Path,
    *,
    create_parents: bool = False,
) -> PortablePathResolution:
    """Resolve every component using NFC/casefold equivalence.

    A unique existing equivalent spelling is reused. Multiple equivalent
    siblings are unsafe and fail closed. Missing components are normalized to
    NFC; when ``create_parents`` is true, missing ancestor directories are
    created one at a time after another equivalence check. The final component
    is never created by this function.
    """

    absolute = _absolute_path(path)
    anchor = Path(absolute.anchor)
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    if not parts:
        return PortablePathResolution(anchor, True)

    current = anchor
    for index, raw_name in enumerate(parts):
        is_final = index == len(parts) - 1
        match = _unique_portable_match(current, raw_name)
        if match is not None:
            if not is_final:
                if _is_redirecting_ancestor(match):
                    raise PortablePathError(
                        f"Portable path ancestor is a symlink or reparse point: {match}"
                    )
                if not match.is_dir():
                    raise PortablePathError(
                        f"Portable path ancestor is not a directory: {match}"
                    )
            current = match
            continue

        normalized = normalize_portable_name(raw_name)
        candidate = current / normalized
        if is_final:
            return PortablePathResolution(candidate, False)
        if not create_parents:
            for remainder in parts[index + 1 :]:
                candidate /= normalize_portable_name(remainder)
            return PortablePathResolution(candidate, False)

        try:
            candidate.mkdir()
        except FileExistsError:
            # Another process may have created an exact or equivalent entry
            # after the lookup. Rediscover it and use only one unambiguous
            # directory.
            match = _unique_portable_match(current, raw_name)
            if (
                match is None
                or _is_redirecting_ancestor(match)
                or not match.is_dir()
            ):
                raise PortablePathError(
                    f"Portable path ancestor is not a directory: {candidate}"
                )
            current = match
        except OSError as exc:
            raise PortablePathError(
                f"Portable path ancestor could not be created: {candidate}"
            ) from exc
        else:
            try:
                confirmed = _unique_portable_match(current, raw_name)
            except PortablePathError:
                # This function created candidate and it is still expected to
                # be empty. Remove it if possible so a racing equivalent name
                # does not leave the parallel tree that we are refusing.
                try:
                    candidate.rmdir()
                except OSError:
                    pass
                raise
            if (
                confirmed is None
                or _is_redirecting_ancestor(confirmed)
                or not confirmed.is_dir()
            ):
                raise PortablePathError(
                    f"Portable path ancestor could not be confirmed: {candidate}"
                )
            current = confirmed

    return PortablePathResolution(current, True)


def portable_path_entry_exists(path: Path) -> bool:
    """Check exact and NFC/casefold-equivalent entries component by component.

    Linux and Windows commonly preserve distinct Unicode spellings while some
    filesystems normalize them. Unique equivalent ancestors are followed so a
    final collision cannot hide behind a differently spelled parent.
    """

    try:
        return resolve_portable_path(path).entry_exists
    except PortablePathError:
        # Boolean callers use a positive result as a safe refusal. Detailed
        # callers use resolve_portable_path() and surface the controlled error.
        return True
