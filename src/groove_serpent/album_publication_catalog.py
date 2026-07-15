"""Restart-safe, read-only discovery of immutable album publication plans."""

from __future__ import annotations

import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .album import canonical_album_path, load_album_project_with_sha256
from .album_publication_executor import preflight_album_publication_plan
from .album_publication_plan import (
    ALBUM_PUBLICATION_PLAN_SCHEMA,
    AlbumPublicationPlan,
    load_album_publication_plan_with_sha256,
)
from .errors import ExportError, ProjectValidationError
from .portable_names import portable_name_key


ALBUM_PUBLICATION_PLAN_CATALOG_SCHEMA = (
    "groove-serpent.album-publication-plan-catalog/1"
)
PUBLICATION_PLAN_FILENAME_SUFFIX = ".publication-plan.json"

_MAX_DIRECTORY_ENTRIES = 4_096
_MAX_PLAN_CANDIDATES = 128
_MAX_LIVE_PREFLIGHTS = 8
_WINDOWS_DEVICE_STEMS = {
    "aux",
    "clock$",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "com6",
    "com7",
    "com8",
    "com9",
    "con",
    "lpt1",
    "lpt2",
    "lpt3",
    "lpt4",
    "lpt5",
    "lpt6",
    "lpt7",
    "lpt8",
    "lpt9",
    "nul",
    "prn",
}


@dataclass(frozen=True, slots=True)
class PublicationPlanIssue:
    """One bounded catalog classification reason."""

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class PublicationPlanCatalogEntry:
    """One immutable sibling plan and its current restart classification."""

    filename: str
    status: str
    file_sha256: str | None
    plan_sha256: str | None
    selected_profiles: tuple[str, ...]
    restoration_mode: str | None
    side_count: int | None
    issues: tuple[PublicationPlanIssue, ...]

    def to_dict(self) -> dict[str, Any]:
        if self.status not in {"current", "stale", "invalid"}:
            raise RuntimeError("Publication catalog entry has an invalid status.")
        return {
            "filename": self.filename,
            "status": self.status,
            "file_sha256": self.file_sha256,
            "plan_sha256": self.plan_sha256,
            "selected_profiles": list(self.selected_profiles),
            "restoration_mode": self.restoration_mode,
            "side_count": self.side_count,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class AlbumPublicationPlanCatalog:
    """A complete bounded snapshot of plans beside one exact album project."""

    album_reference: str
    album_sha256: str
    scan_complete: bool
    entries: tuple[PublicationPlanCatalogEntry, ...]
    issues: tuple[PublicationPlanIssue, ...]
    schema: str = ALBUM_PUBLICATION_PLAN_CATALOG_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        counts = {
            status: sum(entry.status == status for entry in self.entries)
            for status in ("current", "stale", "invalid")
        }
        return {
            "schema": self.schema,
            "album_reference": self.album_reference,
            "album_sha256": self.album_sha256,
            "scan_complete": self.scan_complete,
            "summary": {
                "total": len(self.entries),
                "current": counts["current"],
                "stale": counts["stale"],
                "invalid": counts["invalid"],
            },
            "entries": [entry.to_dict() for entry in self.entries],
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(slots=True)
class _Candidate:
    path: Path
    plan: AlbumPublicationPlan | None
    file_sha256: str | None
    issue: PublicationPlanIssue | None


def _is_reparse(details: os.stat_result) -> bool:
    attributes = getattr(details, "st_file_attributes", 0)
    return isinstance(attributes, int) and bool(attributes & 0x0400)


def _is_plan_convention(name: str) -> bool:
    return name.casefold().endswith(PUBLICATION_PLAN_FILENAME_SUFFIX)


def _portable_filename(name: str) -> bool:
    return bool(
        name
        and len(name) <= 255
        and name == name.strip()
        and unicodedata.normalize("NFC", name) == name
        and not any(ord(character) < 32 for character in name)
        and not any(character in '<>:"/\\|?*' for character in name)
        and not name.endswith((" ", "."))
        and name.split(".", 1)[0].casefold() not in _WINDOWS_DEVICE_STEMS
        and Path(name).suffix.casefold() == ".json"
    )


def _restoration_mode(plan: AlbumPublicationPlan) -> str:
    return (
        "reviewed"
        if any(
            side.restoration_render is not None
            or side.restoration_no_derivative is not None
            for side in plan.sides
        )
        else "none"
    )


def _entry(
    candidate: _Candidate,
    *,
    status: str,
    issue: PublicationPlanIssue | None = None,
) -> PublicationPlanCatalogEntry:
    plan = candidate.plan
    issues = tuple(item for item in (issue or candidate.issue,) if item is not None)
    return PublicationPlanCatalogEntry(
        filename=candidate.path.name,
        status=status,
        file_sha256=candidate.file_sha256,
        plan_sha256=None if plan is None else plan.plan_sha256,
        selected_profiles=() if plan is None else plan.selected_profiles,
        restoration_mode=None if plan is None else _restoration_mode(plan),
        side_count=None if plan is None else len(plan.sides),
        issues=issues,
    )


def _load_candidate(path: Path, album_name: str) -> _Candidate | None:
    convention = _is_plan_convention(path.name)
    try:
        details = path.lstat()
    except OSError:
        if not convention:
            return None
        return _Candidate(
            path,
            None,
            None,
            PublicationPlanIssue(
                "unreadable_plan_entry",
                "The publication-plan entry could not be inspected safely.",
            ),
        )
    if path.is_symlink() or _is_reparse(details) or not stat.S_ISREG(details.st_mode):
        if not convention:
            return None
        return _Candidate(
            path,
            None,
            None,
            PublicationPlanIssue(
                "unsafe_plan_entry",
                "Publication plans must be regular non-symlink, non-reparse files.",
            ),
        )
    try:
        plan, file_sha256 = load_album_publication_plan_with_sha256(path)
    except ProjectValidationError as exc:
        if not convention:
            return None
        message = str(exc)[:1_024] or "The publication plan is malformed."
        return _Candidate(
            path,
            None,
            None,
            PublicationPlanIssue("invalid_plan_document", message),
        )
    if plan.schema != ALBUM_PUBLICATION_PLAN_SCHEMA:
        return None
    if plan.album_reference != album_name:
        return None
    issue = None
    if not _portable_filename(path.name):
        issue = PublicationPlanIssue(
            "nonportable_plan_filename",
            "The publication-plan filename is not canonical portable text.",
        )
    return _Candidate(path, plan, file_sha256, issue)


def _scan_candidates(album_path: Path) -> tuple[list[_Candidate], bool, list[PublicationPlanIssue]]:
    candidates: list[_Candidate] = []
    issues: list[PublicationPlanIssue] = []
    complete = True
    try:
        with os.scandir(album_path.parent) as entries:
            for count, raw_entry in enumerate(entries, start=1):
                if count > _MAX_DIRECTORY_ENTRIES:
                    complete = False
                    issues.append(
                        PublicationPlanIssue(
                            "directory_entry_limit_exceeded",
                            "The album folder has too many entries for complete plan discovery.",
                        )
                    )
                    break
                if raw_entry.name == album_path.name:
                    continue
                if not raw_entry.name.casefold().endswith(".json"):
                    continue
                candidate = _load_candidate(album_path.parent / raw_entry.name, album_path.name)
                if candidate is None:
                    continue
                candidates.append(candidate)
                if len(candidates) > _MAX_PLAN_CANDIDATES:
                    complete = False
                    issues.append(
                        PublicationPlanIssue(
                            "plan_candidate_limit_exceeded",
                            "The album folder has too many publication-plan candidates.",
                        )
                    )
                    candidates = candidates[:_MAX_PLAN_CANDIDATES]
                    break
    except OSError as exc:
        raise ProjectValidationError(
            "The album folder could not be scanned for publication plans."
        ) from exc
    return candidates, complete, issues


def discover_album_publication_plan_catalog(
    album_path: Path,
    *,
    expected_album_sha256: str | None = None,
) -> AlbumPublicationPlanCatalog:
    """Rediscover sibling plans and preflight every exact current candidate.

    Discovery never creates, repairs, replaces, or removes a plan.  A structurally
    valid plan is ``current`` only after the existing execution preflight verifies
    the album, every side/source/speed/restoration binding, and the current tool
    observations.  Valid but mismatched plans are ``stale``; unsafe or malformed
    candidates are ``invalid``.
    """

    canonical = canonical_album_path(album_path)
    _album, album_sha256 = load_album_project_with_sha256(canonical)
    if expected_album_sha256 is not None and album_sha256 != expected_album_sha256:
        raise ProjectValidationError(
            "The album project changed before publication-plan discovery."
        )
    candidates, complete, catalog_issues = _scan_candidates(canonical)
    collisions: set[str] = set()
    by_portable_name: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        by_portable_name.setdefault(
            portable_name_key(candidate.path.name), []
        ).append(candidate)
    for key, matches in by_portable_name.items():
        if len(matches) > 1:
            collisions.add(key)

    result: list[PublicationPlanCatalogEntry] = []
    live_preflights = 0
    preflight_budget_reported = False
    for candidate in sorted(candidates, key=lambda item: portable_name_key(item.path.name)):
        if portable_name_key(candidate.path.name) in collisions:
            result.append(
                _entry(
                    candidate,
                    status="invalid",
                    issue=PublicationPlanIssue(
                        "portable_name_collision",
                        "Portable-equivalent publication-plan names are ambiguous.",
                    ),
                )
            )
            continue
        if candidate.plan is None or candidate.issue is not None:
            result.append(_entry(candidate, status="invalid"))
            continue
        if candidate.plan.album_sha256 != album_sha256:
            result.append(
                _entry(
                    candidate,
                    status="stale",
                    issue=PublicationPlanIssue(
                        "album_identity_changed",
                        "The album project no longer matches this immutable plan.",
                    ),
                )
            )
            continue
        if live_preflights >= _MAX_LIVE_PREFLIGHTS:
            complete = False
            if not preflight_budget_reported:
                catalog_issues.append(
                    PublicationPlanIssue(
                        "live_preflight_limit_exceeded",
                        "Too many matching plans require full live preflight in one "
                        "catalog refresh; excess plans remain fail-closed.",
                    )
                )
                preflight_budget_reported = True
            result.append(
                _entry(
                    candidate,
                    status="stale",
                    issue=PublicationPlanIssue(
                        "live_preflight_not_run",
                        "This plan was not proven current because the bounded live "
                        "preflight limit was reached.",
                    ),
                )
            )
            continue
        live_preflights += 1
        try:
            report = preflight_album_publication_plan(candidate.path)
        except (ExportError, ProjectValidationError, OSError) as exc:
            message = str(exc)[:1_024] or "A bound publication input changed."
            result.append(
                _entry(
                    candidate,
                    status="stale",
                    issue=PublicationPlanIssue("live_preflight_failed", message),
                )
            )
            continue
        try:
            repeated_plan, repeated_file_sha256 = (
                load_album_publication_plan_with_sha256(candidate.path)
            )
        except ProjectValidationError:
            result.append(
                _entry(
                    candidate,
                    status="invalid",
                    issue=PublicationPlanIssue(
                        "plan_changed_during_discovery",
                        "The publication plan changed during read-only discovery.",
                    ),
                )
            )
            continue
        if (
            report.plan_sha256 != candidate.plan.plan_sha256
            or repeated_plan.plan_sha256 != candidate.plan.plan_sha256
            or repeated_file_sha256 != candidate.file_sha256
        ):
            result.append(
                _entry(
                    candidate,
                    status="invalid",
                    issue=PublicationPlanIssue(
                        "plan_changed_during_discovery",
                        "The publication plan changed during read-only discovery.",
                    ),
                )
            )
            continue
        result.append(_entry(candidate, status="current"))

    _repeated_album, repeated_album_sha256 = load_album_project_with_sha256(canonical)
    if repeated_album_sha256 != album_sha256:
        raise ProjectValidationError(
            "The album project changed during publication-plan discovery."
        )
    return AlbumPublicationPlanCatalog(
        album_reference=canonical.name,
        album_sha256=album_sha256,
        scan_complete=complete,
        entries=tuple(result),
        issues=tuple(catalog_issues),
    )


__all__ = [
    "ALBUM_PUBLICATION_PLAN_CATALOG_SCHEMA",
    "PUBLICATION_PLAN_FILENAME_SUFFIX",
    "AlbumPublicationPlanCatalog",
    "PublicationPlanCatalogEntry",
    "PublicationPlanIssue",
    "discover_album_publication_plan_catalog",
]
