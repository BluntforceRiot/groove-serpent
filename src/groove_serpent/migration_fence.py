"""Bounded detection of incomplete migrations before mutable saves.

Migration modules cannot be imported by the project and album serializers
without creating dependency cycles.  This small shared module recognizes only
their fixed, digest-derived pending-journal names.  Callers must hold the
target's write lease while checking so a cooperating migration cannot create
or remove a journal between this check and the save.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal

from .errors import ProjectValidationError


MigrationTargetKind = Literal["project", "album"]
MAX_MIGRATION_DIRECTORY_ENTRIES = 100_000
_PENDING_SUFFIX = ".pending.json"
_PREFIXES: dict[MigrationTargetKind, str] = {
    "project": ".groove-serpent-migration-",
    "album": ".groove-serpent-album-migration-",
}


def _pending_name_bounds(target: Path, kind: MigrationTargetKind) -> tuple[str, str]:
    filename_id = hashlib.sha256(target.name.encode("utf-8")).hexdigest()[:12]
    prefix = f"{_PREFIXES[kind]}{filename_id}-"
    return os.path.normcase(prefix), os.path.normcase(_PENDING_SUFFIX)


def matching_pending_migration(target: Path, kind: MigrationTargetKind) -> str | None:
    """Return one matching sibling journal name, bounded and without following it."""

    prefix, suffix = _pending_name_bounds(target, kind)
    inspected = 0
    try:
        with os.scandir(target.parent) as entries:
            for entry in entries:
                inspected += 1
                if inspected > MAX_MIGRATION_DIRECTORY_ENTRIES:
                    raise ProjectValidationError(
                        "The project directory has too many entries to safely exclude "
                        "an incomplete migration."
                    )
                name = os.path.normcase(entry.name)
                if (
                    name.startswith(prefix)
                    and name.endswith(suffix)
                    and len(name) > len(prefix) + len(suffix)
                ):
                    return entry.name
    except ProjectValidationError:
        raise
    except OSError as exc:
        raise ProjectValidationError(
            "The project directory could not be checked for an incomplete migration."
        ) from exc
    return None


def assert_no_pending_migration(target: Path, kind: MigrationTargetKind) -> None:
    """Refuse a save while an exact target's migration journal remains."""

    pending = matching_pending_migration(target, kind)
    if pending is None:
        return
    if kind == "project":
        command = "groove-serpent project migrate PROJECT"
        label = "project"
    else:
        command = "groove-serpent album migrate ALBUM"
        label = "album"
    raise ProjectValidationError(
        f"An incomplete {label} migration is pending ({pending}); run "
        f"'{command}' to recover it before saving."
    )


__all__ = [
    "MAX_MIGRATION_DIRECTORY_ENTRIES",
    "MigrationTargetKind",
    "assert_no_pending_migration",
    "matching_pending_migration",
]
