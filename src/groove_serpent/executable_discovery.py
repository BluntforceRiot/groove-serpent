"""Resolve external tools without allowing an implicit current-directory search."""

from __future__ import annotations

import os
import re
from pathlib import Path


_WINDOWS_DEFAULT_PATHEXT = (".COM", ".EXE", ".BAT", ".CMD")
_WINDOWS_PATHEXT_RE = re.compile(r"\.[A-Za-z0-9_-]{1,16}\Z")


def _simple_executable_name(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and not any(
        separator in value for separator in ("/", "\\")
    )


def _windows_extensions() -> tuple[str, ...]:
    if os.name != "nt":
        return ()
    raw = os.environ.get("PATHEXT", "")
    extensions: list[str] = []
    for value in raw.split(os.pathsep):
        extension = value.strip()
        if (
            _WINDOWS_PATHEXT_RE.fullmatch(extension) is not None
            and extension.casefold() not in {item.casefold() for item in extensions}
        ):
            extensions.append(extension)
    return tuple(extensions) or _WINDOWS_DEFAULT_PATHEXT


def _candidate_names(name: str) -> tuple[str, ...]:
    extensions = _windows_extensions()
    if not extensions or any(
        name.casefold().endswith(extension.casefold()) for extension in extensions
    ):
        return (name,)
    return tuple(f"{name}{extension}" for extension in extensions)


def _resolved_executable(candidate: Path) -> str | None:
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            return None
        if os.name != "nt" and not os.access(resolved, os.X_OK):
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
    ``NeedCurrentDirectoryForExePath``. An explicit override may name a fully
    qualified path, or a bare executable name that is searched by the same
    rules; relative configured paths are deliberately refused.
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
