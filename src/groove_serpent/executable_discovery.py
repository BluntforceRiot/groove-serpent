"""Resolve native external tools without allowing unsafe search behavior."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath


_WINDOWS_EXECUTABLE_SUFFIX = ".exe"
_WINDOWS_EXECUTABLE_IMAGE = 0x0002
_WINDOWS_OPTIONAL_HEADER_MAGICS = {b"\x0b\x01", b"\x0b\x02"}


def _simple_executable_name(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and not any(
        separator in value for separator in ("/", "\\")
    )


def _windows_candidate_names(name: str) -> tuple[str, ...]:
    suffix = PureWindowsPath(name).suffix
    if not suffix:
        return (f"{name}{_WINDOWS_EXECUTABLE_SUFFIX}",)
    if suffix.casefold() == _WINDOWS_EXECUTABLE_SUFFIX:
        return (name,)
    return ()


def _candidate_names(name: str) -> tuple[str, ...]:
    if os.name != "nt":
        return (name,)
    return _windows_candidate_names(name)


def _is_windows_native_executable(path: Path) -> bool:
    if path.suffix.casefold() != _WINDOWS_EXECUTABLE_SUFFIX:
        return False

    size = path.stat().st_size
    with path.open("rb") as handle:
        dos_header = handle.read(64)
        if len(dos_header) != 64 or dos_header[:2] != b"MZ":
            return False
        pe_offset = int.from_bytes(dos_header[60:64], "little")
        if pe_offset < 64 or pe_offset > size - 26:
            return False

        handle.seek(pe_offset)
        if handle.read(4) != b"PE\x00\x00":
            return False
        coff_header = handle.read(20)
        if len(coff_header) != 20:
            return False
        optional_header_size = int.from_bytes(coff_header[16:18], "little")
        characteristics = int.from_bytes(coff_header[18:20], "little")
        if (
            optional_header_size < 2
            or pe_offset + 24 + optional_header_size > size
            or not characteristics & _WINDOWS_EXECUTABLE_IMAGE
        ):
            return False
        return handle.read(2) in _WINDOWS_OPTIONAL_HEADER_MAGICS


def _resolved_executable(candidate: Path) -> str | None:
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            return None
        if os.name == "nt":
            if not _is_windows_native_executable(resolved):
                return None
        elif not os.access(resolved, os.X_OK):
            return None
    except (OSError, RuntimeError):
        return None
    return str(resolved)


def _explicit_executable(value: str) -> str | None:
    expanded = os.path.expandvars(value.strip())
    if len(expanded) >= 2 and expanded[0] == expanded[-1] == '"':
        expanded = expanded[1:-1]
    candidate = Path(expanded).expanduser()
    if not candidate.is_absolute():
        return None
    direct = _resolved_executable(candidate)
    if direct is not None:
        return direct
    for name in _candidate_names(candidate.name):
        if name == candidate.name:
            continue
        resolved = _resolved_executable(candidate.with_name(name))
        if resolved is not None:
            return resolved
    return None


def find_executable(name: str, *, explicit: str | None = None) -> str | None:
    """Return one resolved executable from explicit input or trusted PATH entries.

    Ordinary discovery considers only absolute PATH directories and never the
    process current directory. Empty and relative PATH entries are ignored, so
    Windows cannot prepend the current directory through
    ``NeedCurrentDirectoryForExePath``. Windows candidates must be ``.exe`` PE
    images; PATHEXT command scripts and custom associations are not considered.
    An explicit override may name a fully qualified path, or a bare executable
    name that is searched by the same rules; relative configured paths are
    deliberately refused.
    """

    search_name = name.strip()
    if not _simple_executable_name(search_name):
        raise ValueError("Executable discovery requires a bare executable name.")

    if explicit is not None and explicit.strip():
        configured = os.path.expandvars(explicit.strip())
        if len(configured) >= 2 and configured[0] == configured[-1] == '"':
            configured = configured[1:-1]
        configured_path = Path(configured).expanduser()
        if configured_path.is_absolute():
            return _explicit_executable(configured)
        if not _simple_executable_name(configured):
            return None
        search_name = configured

    try:
        current_directory = Path.cwd().resolve(strict=True)
    except (OSError, RuntimeError):
        current_directory = None
    current_key = (
        os.path.normcase(str(current_directory)) if current_directory is not None else None
    )

    seen: set[str] = set()
    for raw_entry in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_entry:
            continue
        entry = raw_entry
        if len(entry) >= 2 and entry[0] == entry[-1] == '"':
            entry = entry[1:-1]
        directory = Path(entry)
        if not directory.is_absolute():
            continue
        try:
            resolved_directory = directory.resolve(strict=True)
            if not resolved_directory.is_dir():
                continue
        except (OSError, RuntimeError):
            continue
        directory_key = os.path.normcase(str(resolved_directory))
        if directory_key == current_key or directory_key in seen:
            continue
        seen.add(directory_key)
        for candidate_name in _candidate_names(search_name):
            resolved = _resolved_executable(resolved_directory / candidate_name)
            if resolved is not None:
                return resolved
    return None


__all__ = ["find_executable"]
