"""Restart-safe discovery of final publications and owned orphan stages."""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .album import canonical_album_path, load_album_project_with_sha256
from .album_publication_catalog import AlbumPublicationPlanCatalog
from .album_publication_durability import (
    inventory_album_publication_orphans,
    load_album_publication_journal,
    load_album_publication_manifest,
    verify_album_publication,
)
from .errors import ExportError, ProjectValidationError
from .portable_names import portable_name_key


ALBUM_PUBLICATION_OPERATION_CATALOG_SCHEMA = (
    "groove-serpent.album-publication-operation-catalog/1"
)

_MANIFEST_NAME = "groove-serpent-album-publication.json"
_JOURNAL_NAME = "groove-serpent-publication-journal.json"
_MAX_DIRECTORY_ENTRIES = 4_096
_MAX_PUBLICATION_CANDIDATES = 128
_MAX_LIVE_VERIFICATIONS = 8
_REPARSE_POINT = 0x400
_RESERVED_ORPHAN_NAME = re.compile(
    r"(?:\.groove-serpent-album-publication-[0-9a-f]{32}\.partial|"
    r"\.groove-serpent-album-cleanup-[0-9a-f]{32}\.partial)"
)
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
class PublicationOperationIssue:
    """One bounded read-only operation-catalog classification reason."""

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class PublicationReceiptEntry:
    """One final sibling publication and its current receipt classification."""

    directory_name: str
    status: str
    plan_filename: str | None
    plan_file_sha256: str | None
    plan_sha256: str | None
    album_sha256: str | None
    manifest_sha256: str | None
    journal_sha256: str | None
    artifact_count: int
    issues: tuple[PublicationOperationIssue, ...]

    def to_dict(self) -> dict[str, Any]:
        if self.status not in {"current", "stale", "invalid"}:
            raise RuntimeError("Publication receipt has an invalid status.")
        return {
            "directory_name": self.directory_name,
            "status": self.status,
            "plan_filename": self.plan_filename,
            "plan_file_sha256": self.plan_file_sha256,
            "plan_sha256": self.plan_sha256,
            "album_sha256": self.album_sha256,
            "manifest_sha256": self.manifest_sha256,
            "journal_sha256": self.journal_sha256,
            "artifact_count": self.artifact_count,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class PublicationOrphanEntry:
    """One reserved sibling stage, never actionable without exact ownership."""

    directory_name: str
    kind: str
    owned: bool
    state: str | None
    plan_sha256: str | None
    intended_output_name: str | None
    journal_sha256: str | None
    directory_identity: dict[str, str | None] | None
    file_count: int
    total_size_bytes: int
    issue: str | None
    belongs_to_album: bool
    matches_current_plan: bool
    actionable: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AlbumPublicationOperationCatalog:
    """Bounded direct-sibling final-publication and recovery inventory."""

    album_sha256: str
    scan_complete: bool
    publications: tuple[PublicationReceiptEntry, ...]
    orphans: tuple[PublicationOrphanEntry, ...]
    issues: tuple[PublicationOperationIssue, ...]
    schema: str = ALBUM_PUBLICATION_OPERATION_CATALOG_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        counts = {
            status: sum(item.status == status for item in self.publications)
            for status in ("current", "stale", "invalid")
        }
        return {
            "schema": self.schema,
            "album_sha256": self.album_sha256,
            "scan_complete": self.scan_complete,
            "summary": {
                "publications": len(self.publications),
                "current": counts["current"],
                "stale": counts["stale"],
                "invalid": counts["invalid"],
                "orphans": len(self.orphans),
                "actionable_orphans": sum(item.actionable for item in self.orphans),
                "unsafe_orphans": sum(not item.actionable for item in self.orphans),
            },
            "publications": [item.to_dict() for item in self.publications],
            "orphans": [item.to_dict() for item in self.orphans],
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _is_reparse(details: os.stat_result) -> bool:
    attributes = getattr(details, "st_file_attributes", 0)
    return isinstance(attributes, int) and bool(attributes & _REPARSE_POINT)


def _directory_identity(path: Path, *, label: str = "Directory") -> tuple[int, ...]:
    try:
        details = path.lstat()
    except OSError as exc:
        raise ProjectValidationError(
            f"{label} could not be inspected for publications."
        ) from exc
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(details.st_mode)
        or _is_reparse(details)
    ):
        raise ProjectValidationError(
            f"{label} is not a regular non-reparse directory."
        )
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_mtime_ns,
        details.st_ctime_ns,
        int(getattr(details, "st_birthtime_ns", -1)),
        int(getattr(details, "st_file_attributes", -1)),
    )


def _portable_directory_name(name: str) -> bool:
    return bool(
        name
        and len(name) <= 255
        and name == name.strip()
        and not name.startswith(".")
        and unicodedata.normalize("NFC", name) == name
        and not any(ord(character) < 32 for character in name)
        and not any(character in '<>:"/\\|?*' for character in name)
        and not name.endswith((" ", "."))
        and name.split(".", 1)[0].casefold() not in _WINDOWS_DEVICE_STEMS
    )


def _invalid_receipt(
    path: Path,
    code: str,
    message: str,
) -> PublicationReceiptEntry:
    return PublicationReceiptEntry(
        directory_name=path.name,
        status="invalid",
        plan_filename=None,
        plan_file_sha256=None,
        plan_sha256=None,
        album_sha256=None,
        manifest_sha256=None,
        journal_sha256=None,
        artifact_count=0,
        issues=(PublicationOperationIssue(code, message[:1_024]),),
    )


def _scan_publication_candidates(
    parent: Path,
) -> tuple[list[Path], bool, list[PublicationOperationIssue]]:
    candidates: list[Path] = []
    issues: list[PublicationOperationIssue] = []
    complete = True
    parent_identity = _directory_identity(parent, label="Album folder")
    try:
        with os.scandir(parent) as entries:
            for count, raw_entry in enumerate(entries, start=1):
                if count > _MAX_DIRECTORY_ENTRIES:
                    complete = False
                    issues.append(
                        PublicationOperationIssue(
                            "directory_entry_limit_exceeded",
                            "The album folder has too many entries for complete "
                            "publication discovery.",
                        )
                    )
                    break
                path = parent / raw_entry.name
                if _RESERVED_ORPHAN_NAME.fullmatch(raw_entry.name) is not None:
                    continue
                try:
                    details = path.lstat()
                except OSError:
                    continue
                if (
                    stat.S_ISLNK(details.st_mode)
                    or _is_reparse(details)
                    or not stat.S_ISDIR(details.st_mode)
                ):
                    continue
                if not (
                    os.path.lexists(path / _MANIFEST_NAME)
                    or os.path.lexists(path / _JOURNAL_NAME)
                ):
                    continue
                candidates.append(path)
                if len(candidates) > _MAX_PUBLICATION_CANDIDATES:
                    complete = False
                    issues.append(
                        PublicationOperationIssue(
                            "publication_candidate_limit_exceeded",
                            "The album folder has too many final-publication candidates.",
                        )
                    )
                    candidates = candidates[:_MAX_PUBLICATION_CANDIDATES]
                    break
    except OSError as exc:
        raise ProjectValidationError(
            "The album folder could not be scanned for final publications."
        ) from exc
    if _directory_identity(parent, label="Album folder") != parent_identity:
        raise ProjectValidationError(
            "The album folder changed during final-publication discovery."
        )
    return candidates, complete, issues


def _verified_receipt(
    path: Path,
    *,
    album_sha256: str,
    current_plan_keys: set[tuple[str, str, str]],
) -> PublicationReceiptEntry:
    try:
        before_identity = _directory_identity(
            path,
            label="Publication directory",
        )
    except ProjectValidationError as exc:
        return _invalid_receipt(
            path,
            "unsafe_publication_directory",
            str(exc),
        )
    report = verify_album_publication(path)
    if not report.ok:
        message = (
            report.mismatches[0].message
            if report.mismatches
            else "Strict publication verification failed."
        )
        return _invalid_receipt(path, "verification_failed", message)
    try:
        manifest, manifest_receipt = load_album_publication_manifest(
            path / _MANIFEST_NAME
        )
        journal, journal_receipt, _identity = load_album_publication_journal(
            path / _JOURNAL_NAME
        )
        plan = manifest["plan"]
        album = manifest["album"]
        if not isinstance(plan, dict) or not isinstance(album, dict):
            raise ExportError("Verified publication identities are malformed.")
        plan_filename = str(plan["sibling_filename"])
        plan_file_sha256 = str(plan["raw_file_sha256"])
        plan_sha256 = str(plan["plan_sha256"])
        receipt_album_sha256 = str(album["sha256"])
        if (
            report.manifest_sha256 != manifest_receipt.sha256
            or report.journal_sha256 != journal_receipt.sha256
            or journal["plan_sha256"] != plan_sha256
            or _directory_identity(
                path,
                label="Publication directory",
            )
            != before_identity
        ):
            raise ExportError(
                "Publication receipts changed during read-only discovery."
            )
    except (
        ExportError,
        KeyError,
        OSError,
        ProjectValidationError,
        TypeError,
        ValueError,
    ) as exc:
        return _invalid_receipt(
            path,
            "receipt_changed_during_discovery",
            str(exc) or "Publication receipts changed during discovery.",
        )
    exact_current = (
        receipt_album_sha256 == album_sha256
        and (plan_filename, plan_file_sha256, plan_sha256) in current_plan_keys
    )
    issues: tuple[PublicationOperationIssue, ...] = ()
    status = "current" if exact_current else "stale"
    if not exact_current:
        issues = (
            PublicationOperationIssue(
                "publication_not_current",
                "The publication is intact, but its album or sibling plan identity "
                "is no longer current.",
            ),
        )
    return PublicationReceiptEntry(
        directory_name=path.name,
        status=status,
        plan_filename=plan_filename,
        plan_file_sha256=plan_file_sha256,
        plan_sha256=plan_sha256,
        album_sha256=receipt_album_sha256,
        manifest_sha256=manifest_receipt.sha256,
        journal_sha256=journal_receipt.sha256,
        artifact_count=report.artifact_count,
        issues=issues,
    )


def discover_album_publication_operations(
    album_path: Path,
    plan_catalog: AlbumPublicationPlanCatalog,
    *,
    expected_album_sha256: str | None = None,
) -> AlbumPublicationOperationCatalog:
    """Rediscover final receipts and reserved stages without changing either."""

    canonical = canonical_album_path(album_path)
    _album, album_sha256 = load_album_project_with_sha256(canonical)
    if (
        (expected_album_sha256 is not None and album_sha256 != expected_album_sha256)
        or plan_catalog.album_sha256 != album_sha256
    ):
        raise ProjectValidationError(
            "The album project changed before publication-operation discovery."
        )
    candidates, complete, issues = _scan_publication_candidates(canonical.parent)
    current_plan_keys = {
        (entry.filename, entry.file_sha256, entry.plan_sha256)
        for entry in plan_catalog.entries
        if entry.status == "current"
        and entry.file_sha256 is not None
        and entry.plan_sha256 is not None
    }
    known_plan_sha256 = {
        entry.plan_sha256
        for entry in plan_catalog.entries
        if entry.plan_sha256 is not None
    }
    current_plan_sha256 = {
        entry.plan_sha256
        for entry in plan_catalog.entries
        if entry.status == "current" and entry.plan_sha256 is not None
    }

    collisions = {
        key
        for key, count in (
            (key, sum(portable_name_key(item.name) == key for item in candidates))
            for key in {portable_name_key(item.name) for item in candidates}
        )
        if count > 1
    }
    publications: list[PublicationReceiptEntry] = []
    live_verifications = 0
    verification_limit_reported = False
    for path in sorted(candidates, key=lambda item: portable_name_key(item.name)):
        if portable_name_key(path.name) in collisions:
            publications.append(
                _invalid_receipt(
                    path,
                    "portable_name_collision",
                    "Portable-equivalent publication directory names are ambiguous.",
                )
            )
            continue
        if not _portable_directory_name(path.name):
            publications.append(
                _invalid_receipt(
                    path,
                    "nonportable_publication_name",
                    "The publication directory name is not canonical portable text.",
                )
            )
            continue
        if live_verifications >= _MAX_LIVE_VERIFICATIONS:
            complete = False
            if not verification_limit_reported:
                issues.append(
                    PublicationOperationIssue(
                        "live_verification_limit_exceeded",
                        "Too many final publications require full verification in one "
                        "refresh; excess entries remain fail-closed.",
                    )
                )
                verification_limit_reported = True
            publications.append(
                _invalid_receipt(
                    path,
                    "live_verification_not_run",
                    "This publication was not proven intact because the bounded live "
                    "verification limit was reached.",
                )
            )
            continue
        live_verifications += 1
        publications.append(
            _verified_receipt(
                path,
                album_sha256=album_sha256,
                current_plan_keys=current_plan_keys,
            )
        )

    orphan_entries: list[PublicationOrphanEntry] = []
    try:
        orphan_inventory = inventory_album_publication_orphans(canonical.parent)
    except ExportError as exc:
        complete = False
        issues.append(
            PublicationOperationIssue(
                "orphan_inventory_failed",
                (str(exc) or "Publication orphan inventory failed.")[:1_024],
            )
        )
    else:
        if orphan_inventory.truncated:
            complete = False
            issues.append(
                PublicationOperationIssue(
                    "orphan_inventory_truncated",
                    "The bounded publication-orphan inventory is incomplete.",
                )
            )
        for orphan in orphan_inventory.orphans:
            path = Path(orphan.path)
            if path.parent != canonical.parent:
                raise ProjectValidationError(
                    "Publication orphan inventory escaped the album folder."
                )
            belongs = orphan.plan_sha256 in known_plan_sha256
            matches_current = orphan.plan_sha256 in current_plan_sha256
            identity = (
                None
                if orphan.directory_identity is None
                else {
                    key: None if value is None else str(value)
                    for key, value in asdict(orphan.directory_identity).items()
                }
            )
            actionable = bool(
                orphan.owned
                and belongs
                and orphan.journal_sha256 is not None
                and identity is not None
            )
            orphan_entries.append(
                PublicationOrphanEntry(
                    directory_name=path.name,
                    kind=orphan.kind,
                    owned=orphan.owned,
                    state=orphan.state,
                    plan_sha256=orphan.plan_sha256,
                    intended_output_name=orphan.intended_output_name,
                    journal_sha256=orphan.journal_sha256,
                    directory_identity=identity,
                    file_count=orphan.file_count,
                    total_size_bytes=orphan.total_size_bytes,
                    issue=orphan.issue,
                    belongs_to_album=belongs,
                    matches_current_plan=matches_current,
                    actionable=actionable,
                )
            )

    _repeated_album, repeated_album_sha256 = load_album_project_with_sha256(canonical)
    if repeated_album_sha256 != album_sha256:
        raise ProjectValidationError(
            "The album project changed during publication-operation discovery."
        )
    return AlbumPublicationOperationCatalog(
        album_sha256=album_sha256,
        scan_complete=complete and plan_catalog.scan_complete,
        publications=tuple(publications),
        orphans=tuple(
            sorted(orphan_entries, key=lambda item: portable_name_key(item.directory_name))
        ),
        issues=tuple(issues),
    )


__all__ = [
    "ALBUM_PUBLICATION_OPERATION_CATALOG_SCHEMA",
    "AlbumPublicationOperationCatalog",
    "PublicationOperationIssue",
    "PublicationOrphanEntry",
    "PublicationReceiptEntry",
    "discover_album_publication_operations",
]
