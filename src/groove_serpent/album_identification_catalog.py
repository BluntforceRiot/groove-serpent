"""Immutable persistence and restart discovery for album-identification proposals.

Only semantically validated proposal-only JSON is written.  Files are created
directly beside their exact album project with a content-derived portable name
and an atomic no-replace commit.  Discovery is read-only and classifies each
candidate as current, stale, or invalid.  A current catalog entry is still only
selectable for review: this module has no metadata, artwork, project, or network
authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, cast

from . import __version__
from . import album_identification as identification_module
from .album import canonical_album_path, load_album_project_with_sha256
from .album_identification import (
    ALBUM_IDENTIFICATION_ALGORITHM,
    ALBUM_IDENTIFICATION_PROPOSAL_SCHEMA,
    AlbumIdentificationConfig,
    AlbumIdentificationContext,
    capture_album_identification_context,
    validate_album_identification_proposal,
)
from .atomic_create import rename_no_replace
from .errors import ExportError, ProjectValidationError
from .portable_names import PortablePathError, portable_name_key, resolve_portable_path
from .publication import canonical_json_sha256, capture_file_receipt, same_file_object_stats


ALBUM_IDENTIFICATION_CATALOG_SCHEMA = "groove-serpent.album-identification-catalog/1"
PROPOSAL_FILENAME_PREFIX = "album-identification-"
PROPOSAL_FILENAME_SUFFIX = ".proposal.json"
PROPOSAL_FILENAME_RE = re.compile(
    r"^album-identification-([0-9a-f]{64})\.proposal\.json$"
)

MAX_PROPOSAL_BYTES = 24 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 4_096
MAX_PROPOSAL_CANDIDATES = 64
MAX_CATALOG_BYTES = 64 * 1024 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_VALUES = 1_000_000
MAX_JSON_STRING = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024
_REPARSE_POINT = 0x400
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class _InvalidProposal(ProjectValidationError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class IdentificationCatalogIssue:
    """One bounded restart-classification reason."""

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class AlbumIdentificationCatalogEntry:
    """One immutable sibling proposal and its live classification."""

    filename: str
    status: str
    selectable: bool
    file_sha256: str | None
    proposal_sha256: str | None
    decision_status: str | None
    confidence: str | None
    selected_release_mbid: str | None
    manual_candidate_count: int | None
    issues: tuple[IdentificationCatalogIssue, ...]

    def to_dict(self) -> dict[str, Any]:
        if self.status not in {"current", "stale", "invalid"}:
            raise RuntimeError("Identification catalog entry status is invalid.")
        if self.selectable != (self.status == "current"):
            raise RuntimeError("Only current identification proposals are selectable.")
        return {
            "filename": self.filename,
            "status": self.status,
            "selectable": self.selectable,
            "file_sha256": self.file_sha256,
            "proposal_sha256": self.proposal_sha256,
            "decision_status": self.decision_status,
            "confidence": self.confidence,
            "selected_release_mbid": self.selected_release_mbid,
            "manual_candidate_count": self.manual_candidate_count,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class AlbumIdentificationProposalCatalog:
    """Bounded restart inventory for one exact album-project sibling folder."""

    album_reference: str
    album_sha256: str
    live_context_available: bool
    scan_complete: bool
    entries: tuple[AlbumIdentificationCatalogEntry, ...]
    issues: tuple[IdentificationCatalogIssue, ...]
    schema: str = ALBUM_IDENTIFICATION_CATALOG_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        counts = {
            status: sum(entry.status == status for entry in self.entries)
            for status in ("current", "stale", "invalid")
        }
        selectable = sum(entry.selectable for entry in self.entries)
        return {
            "schema": self.schema,
            "album_reference": self.album_reference,
            "album_sha256": self.album_sha256,
            "live_context_available": self.live_context_available,
            "scan_complete": self.scan_complete,
            "summary": {
                "total": len(self.entries),
                "current": counts["current"],
                "stale": counts["stale"],
                "invalid": counts["invalid"],
                "selectable": selectable,
            },
            "entries": [entry.to_dict() for entry in self.entries],
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class LoadedAlbumIdentificationProposal:
    """A strict immutable file load, not proof that its bindings remain current."""

    path: Path
    proposal: dict[str, Any]
    file_sha256: str
    raw: bytes
    identity: tuple[int | None, ...]


@dataclass(frozen=True, slots=True)
class _RuntimeIdentity:
    algorithm_id: str
    module_name: str
    module_sha256: str
    app_version: str
    config_values: dict[str, Any]
    config_sha256: str


@dataclass(slots=True)
class _Candidate:
    path: Path
    loaded: LoadedAlbumIdentificationProposal | None
    issue: IdentificationCatalogIssue | None


def _invalid(code: str, message: str) -> _InvalidProposal:
    return _InvalidProposal(code, message)


def _is_reparse(value: os.stat_result) -> bool:
    return bool(int(getattr(value, "st_file_attributes", 0)) & _REPARSE_POINT)


def _file_identity(value: os.stat_result) -> tuple[int | None, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_nlink),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
        (
            int(getattr(value, "st_birthtime_ns"))
            if getattr(value, "st_birthtime_ns", None) is not None
            else None
        ),
        (
            int(getattr(value, "st_file_attributes"))
            if getattr(value, "st_file_attributes", None) is not None
            else None
        ),
    )


def _plain_file(path: Path) -> os.stat_result:
    try:
        details = path.lstat()
    except OSError as exc:
        raise _invalid(
            "unreadable_proposal_entry",
            "The identification-proposal entry could not be inspected.",
        ) from exc
    if path.is_symlink() or _is_reparse(details):
        raise _invalid(
            "unsafe_reparse_entry",
            "Identification proposals cannot be symlinks, junctions, or reparse points.",
        )
    if not stat.S_ISREG(details.st_mode) or int(details.st_nlink) != 1:
        raise _invalid(
            "unsafe_file_alias",
            "Identification proposals must be single-link regular files.",
        )
    return details


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Invalid JSON number: {value}")
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def _validate_json_envelope(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    values_seen = 0
    while stack:
        item, depth = stack.pop()
        values_seen += 1
        if values_seen > MAX_JSON_VALUES:
            raise _invalid(
                "json_value_limit",
                "The proposal exceeds the bounded JSON value count.",
            )
        if depth > MAX_JSON_DEPTH:
            raise _invalid(
                "json_depth_limit",
                "The proposal exceeds the bounded JSON nesting depth.",
            )
        if isinstance(item, str) and len(item) > MAX_JSON_STRING:
            raise _invalid(
                "json_string_limit",
                "The proposal contains an unbounded JSON string.",
            )
        if type(item) is dict:
            mapping = cast(dict[str, Any], item)
            stack.extend((key, depth + 1) for key in mapping)
            stack.extend((child, depth + 1) for child in mapping.values())
        elif type(item) is list:
            stack.extend((child, depth + 1) for child in item)


def _proposal_bytes(proposal: Mapping[str, Any]) -> bytes:
    try:
        rendered = json.dumps(
            proposal,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ProjectValidationError(
            f"Identification proposal is not deterministic finite JSON: {exc}"
        ) from exc
    raw = (rendered + "\n").encode("utf-8")
    if len(raw) > MAX_PROPOSAL_BYTES:
        raise ProjectValidationError(
            f"Identification proposal exceeds {MAX_PROPOSAL_BYTES} serialized bytes."
        )
    return raw


def _body_hash_is_valid(proposal: Mapping[str, Any]) -> bool:
    digest = proposal.get("proposal_sha256")
    if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        return False
    body = {key: proposal[key] for key in proposal if key != "proposal_sha256"}
    try:
        return canonical_json_sha256(body) == digest
    except (TypeError, ValueError):
        return False


def _validate_catalog_semantics(proposal: dict[str, Any]) -> None:
    """Validate current semantics while allowing a historical algorithm marker.

    A historical algorithm ID/module is never selectable.  Normalizing only
    those two marker strings lets the current validator prove that identities,
    evidence, ranking, decision, manual review, and no-authority invariants are
    structurally coherent before cataloging the file as stale rather than valid.
    """

    try:
        validate_album_identification_proposal(proposal)
        return
    except ProjectValidationError as original:
        algorithm = proposal.get("algorithm")
        if type(algorithm) is not dict or not _body_hash_is_valid(proposal):
            raise original
        algorithm_dict = cast(dict[str, Any], algorithm)
        if set(algorithm_dict) != {"id", "module", "module_sha256", "app_version"}:
            raise original
        for key in ("id", "module"):
            value = algorithm_dict[key]
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 256
                or any(ord(character) < 32 for character in value)
            ):
                raise original
        if (
            algorithm_dict["id"] == ALBUM_IDENTIFICATION_ALGORITHM
            and algorithm_dict["module"] == "groove_serpent.album_identification"
        ):
            raise original
        normalized = copy.deepcopy(proposal)
        normalized_algorithm = cast(dict[str, Any], normalized["algorithm"])
        normalized_algorithm["id"] = ALBUM_IDENTIFICATION_ALGORITHM
        normalized_algorithm["module"] = "groove_serpent.album_identification"
        normalized_body = {
            key: normalized[key] for key in normalized if key != "proposal_sha256"
        }
        normalized["proposal_sha256"] = canonical_json_sha256(normalized_body)
        validate_album_identification_proposal(normalized)


def load_album_identification_proposal_file(
    path: Path,
) -> LoadedAlbumIdentificationProposal:
    """Strictly load one stable, duplicate-free, bounded proposal file."""

    supplied = Path(os.path.abspath(os.fspath(path.expanduser())))
    try:
        before = _plain_file(supplied)
        if before.st_size <= 0 or before.st_size > MAX_PROPOSAL_BYTES:
            raise _invalid(
                "proposal_size_limit",
                f"Identification proposals must contain 1-{MAX_PROPOSAL_BYTES} bytes.",
            )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(supplied, flags)
        try:
            opened_before = os.fstat(descriptor)
            if not same_file_object_stats(opened_before, before):
                raise _invalid(
                    "proposal_changed",
                    "The identification proposal changed before it was opened.",
                )
            chunks: list[bytes] = []
            observed = 0
            while True:
                chunk = os.read(descriptor, _READ_CHUNK_BYTES)
                if not chunk:
                    break
                observed += len(chunk)
                if observed > MAX_PROPOSAL_BYTES:
                    raise _invalid(
                        "proposal_size_limit",
                        "The identification proposal grew beyond its size limit.",
                    )
                chunks.append(chunk)
            opened_after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = _plain_file(supplied)
        raw = b"".join(chunks)
        if (
            _file_identity(before) != _file_identity(after)
            or _file_identity(opened_before) != _file_identity(opened_after)
            or not same_file_object_stats(opened_after, after)
            or len(raw) != before.st_size
        ):
            raise _invalid(
                "proposal_changed",
                "The identification proposal changed while it was read.",
            )
        try:
            decoded = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
            )
        except (RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise _invalid(
                "invalid_json",
                "The proposal is not strict finite duplicate-free JSON.",
            ) from exc
        if type(decoded) is not dict:
            raise _invalid("invalid_schema", "The proposal root must be a JSON object.")
        proposal = cast(dict[str, Any], decoded)
        _validate_json_envelope(proposal)
        try:
            _validate_catalog_semantics(proposal)
        except ProjectValidationError as exc:
            raise _invalid(
                "invalid_proposal_semantics",
                str(exc)[:1_024] or "The proposal is semantically invalid.",
            ) from exc
        if proposal.get("schema") != ALBUM_IDENTIFICATION_PROPOSAL_SCHEMA:
            raise _invalid("invalid_schema", "The proposal schema is unsupported.")
        if raw != _proposal_bytes(proposal):
            raise _invalid(
                "noncanonical_serialization",
                "Persisted identification proposals must use canonical serialization.",
            )
        return LoadedAlbumIdentificationProposal(
            path=supplied,
            proposal=proposal,
            file_sha256=hashlib.sha256(raw).hexdigest(),
            raw=raw,
            identity=_file_identity(after),
        )
    except _InvalidProposal:
        raise
    except OSError as exc:
        raise _invalid(
            "unreadable_proposal",
            "The identification proposal could not be read safely.",
        ) from exc


def _runtime_identity(config: AlbumIdentificationConfig | None) -> _RuntimeIdentity:
    selected = config or AlbumIdentificationConfig()
    selected.validate()
    values = selected.to_dict()
    module_file = Path(identification_module.__file__)
    try:
        receipt = capture_file_receipt(
            module_file,
            label="Album identification algorithm module",
        )
    except ExportError as exc:
        raise ProjectValidationError(
            "The current identification algorithm module could not be verified."
        ) from exc
    return _RuntimeIdentity(
        algorithm_id=ALBUM_IDENTIFICATION_ALGORITHM,
        module_name="groove_serpent.album_identification",
        module_sha256=receipt.sha256,
        app_version=__version__,
        config_values=values,
        config_sha256=canonical_json_sha256(values),
    )


def _proposal_hash(proposal: Mapping[str, Any]) -> str:
    value = proposal.get("proposal_sha256")
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ProjectValidationError(
            "Identification proposal SHA-256 must be lowercase hexadecimal text."
        )
    return value


def album_identification_proposal_path(
    album_path: Path,
    proposal: Mapping[str, Any],
) -> Path:
    """Return the deterministic direct-sibling path for one proposal digest."""

    canonical = canonical_album_path(album_path)
    digest = _proposal_hash(proposal)
    return canonical.parent / f"{PROPOSAL_FILENAME_PREFIX}{digest}{PROPOSAL_FILENAME_SUFFIX}"


def _current_issues(
    proposal: Mapping[str, Any],
    *,
    album_name: str,
    context: AlbumIdentificationContext | None,
    runtime: _RuntimeIdentity,
    context_error: str | None,
) -> list[IdentificationCatalogIssue]:
    issues: list[IdentificationCatalogIssue] = []
    album = proposal.get("album")
    if type(album) is not dict:
        return [
            IdentificationCatalogIssue(
                "invalid_album_identity",
                "The proposal lacks a valid album identity.",
            )
        ]
    album_dict = cast(dict[str, Any], album)
    if album_dict.get("album_reference") != album_name:
        issues.append(
            IdentificationCatalogIssue(
                "album_reference_changed",
                "The proposal belongs to a different album-project filename.",
            )
        )
    if context is None:
        issues.append(
            IdentificationCatalogIssue(
                "live_context_unavailable",
                context_error or "The live album context could not be verified.",
            )
        )
    elif album_dict != context.identity_dict():
        issues.append(
            IdentificationCatalogIssue(
                "album_or_side_identity_changed",
                "Album, project, source, speed, or track identities changed.",
            )
        )

    algorithm = cast(dict[str, Any], proposal.get("algorithm", {}))
    if algorithm.get("id") != runtime.algorithm_id:
        issues.append(
            IdentificationCatalogIssue(
                "algorithm_changed",
                "The proposal was produced by a different identification algorithm.",
            )
        )
    if algorithm.get("module") != runtime.module_name:
        issues.append(
            IdentificationCatalogIssue(
                "algorithm_module_changed",
                "The proposal names a different identification module.",
            )
        )
    if algorithm.get("module_sha256") != runtime.module_sha256:
        issues.append(
            IdentificationCatalogIssue(
                "algorithm_module_bytes_changed",
                "The identification module bytes changed after proposal creation.",
            )
        )
    if algorithm.get("app_version") != runtime.app_version:
        issues.append(
            IdentificationCatalogIssue(
                "application_version_changed",
                "The Groove Serpent application version changed.",
            )
        )
    config = proposal.get("config")
    if type(config) is not dict:
        issues.append(
            IdentificationCatalogIssue(
                "config_changed",
                "The proposal does not contain the current identification config.",
            )
        )
    else:
        config_dict = cast(dict[str, Any], config)
        if (
            config_dict.get("values") != runtime.config_values
            or config_dict.get("sha256") != runtime.config_sha256
        ):
            issues.append(
                IdentificationCatalogIssue(
                    "config_changed",
                    "Identification thresholds changed after proposal creation.",
                )
            )
    return issues


def _proposal_album_reference(proposal: Mapping[str, Any]) -> str | None:
    album = proposal.get("album")
    if type(album) is not dict:
        return None
    value = cast(dict[str, Any], album).get("album_reference")
    return value if isinstance(value, str) else None


def _manual_candidate_count(proposal: Mapping[str, Any]) -> int | None:
    pressing = proposal.get("exact_pressing_review")
    if type(pressing) is not dict:
        return None
    candidates = cast(dict[str, Any], pressing).get("manual_candidates")
    return len(candidates) if isinstance(candidates, list) else None


def _entry(
    candidate: _Candidate,
    *,
    status: str,
    issues: tuple[IdentificationCatalogIssue, ...] = (),
) -> AlbumIdentificationCatalogEntry:
    loaded = candidate.loaded
    proposal = loaded.proposal if loaded is not None else {}
    decision = proposal.get("decision")
    decision_dict = cast(dict[str, Any], decision) if type(decision) is dict else {}
    merged_issues = tuple(
        item for item in ((candidate.issue,) + issues) if item is not None
    )
    return AlbumIdentificationCatalogEntry(
        filename=candidate.path.name,
        status=status,
        selectable=status == "current",
        file_sha256=None if loaded is None else loaded.file_sha256,
        proposal_sha256=(
            None if loaded is None else cast(str, proposal.get("proposal_sha256"))
        ),
        decision_status=(
            None if loaded is None else cast(str | None, decision_dict.get("status"))
        ),
        confidence=(
            None if loaded is None else cast(str | None, decision_dict.get("confidence"))
        ),
        selected_release_mbid=(
            None
            if loaded is None
            else cast(str | None, decision_dict.get("selected_release_mbid"))
        ),
        manual_candidate_count=(
            None if loaded is None else _manual_candidate_count(proposal)
        ),
        issues=merged_issues,
    )


def _candidate_from_path(path: Path, *, byte_budget: list[int]) -> _Candidate:
    try:
        details = _plain_file(path)
        if details.st_size > MAX_PROPOSAL_BYTES:
            raise _invalid(
                "proposal_size_limit",
                "The identification proposal exceeds its file-size limit.",
            )
        byte_budget[0] -= int(details.st_size)
        if byte_budget[0] < 0:
            raise _invalid(
                "catalog_byte_budget_exceeded",
                "The bounded catalog byte budget was exhausted.",
            )
        loaded = load_album_identification_proposal_file(path)
        match = PROPOSAL_FILENAME_RE.fullmatch(path.name)
        if match is None:
            raise _invalid(
                "noncanonical_proposal_filename",
                "The proposal filename is not canonical portable text.",
            )
        if match.group(1) != loaded.proposal.get("proposal_sha256"):
            raise _invalid(
                "filename_identity_mismatch",
                "The proposal filename does not match its proposal SHA-256.",
            )
        if unicodedata.normalize("NFC", path.name) != path.name:
            raise _invalid(
                "nonportable_proposal_filename",
                "The proposal filename is not NFC-normalized.",
            )
        return _Candidate(path, loaded, None)
    except _InvalidProposal as exc:
        return _Candidate(
            path,
            None,
            IdentificationCatalogIssue(exc.code, str(exc)[:1_024]),
        )


def _is_convention(name: str) -> bool:
    folded = name.casefold()
    return folded.startswith(PROPOSAL_FILENAME_PREFIX) and folded.endswith(
        PROPOSAL_FILENAME_SUFFIX
    )


def _scan_candidate_paths(
    parent: Path,
) -> tuple[list[Path], bool, list[IdentificationCatalogIssue]]:
    issues: list[IdentificationCatalogIssue] = []
    names: list[str] = []
    try:
        with os.scandir(parent) as entries:
            for count, entry in enumerate(entries, start=1):
                if count > MAX_DIRECTORY_ENTRIES:
                    return (
                        [],
                        False,
                        [
                            IdentificationCatalogIssue(
                                "directory_entry_limit_exceeded",
                                "The album folder is too large for complete proposal discovery.",
                            )
                        ],
                    )
                if _is_convention(entry.name):
                    names.append(entry.name)
    except OSError as exc:
        raise ProjectValidationError(
            "The album folder could not be scanned for identification proposals."
        ) from exc
    names.sort(key=lambda value: (portable_name_key(value), value))
    complete = True
    if len(names) > MAX_PROPOSAL_CANDIDATES:
        complete = False
        issues.append(
            IdentificationCatalogIssue(
                "proposal_candidate_limit_exceeded",
                "The album folder has too many identification proposals.",
            )
        )
        names = names[:MAX_PROPOSAL_CANDIDATES]
    return [parent / name for name in names], complete, issues


def _assert_direct_sibling(album_path: Path, proposal_path: Path) -> None:
    canonical = canonical_album_path(album_path)
    supplied = Path(os.path.abspath(os.fspath(proposal_path.expanduser())))
    if supplied.parent != canonical.parent:
        raise ProjectValidationError(
            "Identification proposals must be direct siblings of the album project."
        )


def _assert_portable_destination_absent(destination: Path) -> None:
    try:
        resolution = resolve_portable_path(destination)
    except PortablePathError as exc:
        raise ProjectValidationError(
            "The proposal destination has an ambiguous or unsafe portable path."
        ) from exc
    if resolution.entry_exists:
        raise ProjectValidationError(
            f"Identification proposal already exists: {resolution.path}."
        )


def _assert_published_portable_identity(destination: Path) -> None:
    try:
        resolution = resolve_portable_path(destination)
    except PortablePathError as exc:
        raise ProjectValidationError(
            "The published proposal has a portable-name collision."
        ) from exc
    if not resolution.entry_exists or resolution.path.name != destination.name:
        raise ProjectValidationError(
            "The published proposal no longer has its exact portable filename."
        )
    try:
        _plain_file(destination)
    except _InvalidProposal as exc:
        raise ProjectValidationError(str(exc)) from exc


def _fsync_parent(parent: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_album_identification_proposal(
    album_path: Path,
    proposal: Mapping[str, Any],
    *,
    config: AlbumIdentificationConfig | None = None,
) -> Path:
    """Atomically persist one exact current proposal without overwrite authority."""

    if type(proposal) is not dict:
        raise ProjectValidationError("Identification proposal must be a JSON object.")
    snapshot = copy.deepcopy(cast(dict[str, Any], proposal))
    validate_album_identification_proposal(snapshot)
    canonical = canonical_album_path(album_path)
    context = capture_album_identification_context(canonical)
    runtime = _runtime_identity(config)
    issues = _current_issues(
        snapshot,
        album_name=canonical.name,
        context=context,
        runtime=runtime,
        context_error=None,
    )
    if issues:
        raise ProjectValidationError(
            "Only a current exact identification proposal can be persisted: "
            + "; ".join(issue.code for issue in issues)
        )
    destination = album_identification_proposal_path(canonical, snapshot)
    _assert_direct_sibling(canonical, destination)
    _assert_portable_destination_absent(destination)
    raw = _proposal_bytes(snapshot)

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    temporary_identity: tuple[int | None, ...] | None = None
    published = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            written = handle.write(raw)
            if written != len(raw):
                raise OSError("short write while staging identification proposal")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_identity = _file_identity(temporary.lstat())
        repeated_context = capture_album_identification_context(canonical)
        repeated_runtime = _runtime_identity(config)
        if repeated_context.sha256 != context.sha256 or repeated_runtime != runtime:
            raise ProjectValidationError(
                "Album or identification runtime changed before proposal publication."
            )
        _assert_portable_destination_absent(destination)
        try:
            rename_no_replace(temporary, destination)
        except FileExistsError as exc:
            raise ProjectValidationError(
                f"Identification proposal already exists: {destination}."
            ) from exc
        except OSError as exc:
            raise ProjectValidationError(
                "The filesystem cannot atomically create a no-overwrite proposal."
            ) from exc
        published = True
        _fsync_parent(destination.parent)
        _assert_published_portable_identity(destination)
        loaded = load_album_identification_proposal_file(destination)
        if loaded.proposal != snapshot or loaded.raw != raw:
            raise ProjectValidationError(
                "The published identification proposal failed exact verification."
            )
        final_context = capture_album_identification_context(canonical)
        final_runtime = _runtime_identity(config)
        if final_context.sha256 != context.sha256 or final_runtime != runtime:
            raise ProjectValidationError(
                "Album or identification runtime changed during proposal publication."
            )
        return destination
    finally:
        if not published and os.path.lexists(temporary):
            try:
                current = temporary.lstat()
                if (
                    temporary_identity is not None
                    and _file_identity(current) == temporary_identity
                ):
                    temporary.unlink()
            except OSError:
                pass


def discover_album_identification_proposal_catalog(
    album_path: Path,
    *,
    config: AlbumIdentificationConfig | None = None,
    expected_album_sha256: str | None = None,
) -> AlbumIdentificationProposalCatalog:
    """Rediscover and classify immutable direct-sibling proposals after restart."""

    canonical = canonical_album_path(album_path)
    _album, album_sha256 = load_album_project_with_sha256(canonical)
    if expected_album_sha256 is not None and album_sha256 != expected_album_sha256:
        raise ProjectValidationError(
            "The album project changed before identification-proposal discovery."
        )
    runtime = _runtime_identity(config)
    context: AlbumIdentificationContext | None
    context_error: str | None = None
    try:
        context = capture_album_identification_context(canonical)
    except ProjectValidationError as exc:
        context = None
        context_error = str(exc)[:1_024]

    paths, complete, catalog_issues = _scan_candidate_paths(canonical.parent)
    candidates: list[_Candidate] = []
    byte_budget = [MAX_CATALOG_BYTES]
    for path in paths:
        candidate = _candidate_from_path(path, byte_budget=byte_budget)
        if candidate.issue is not None and candidate.issue.code == "catalog_byte_budget_exceeded":
            complete = False
            catalog_issues.append(candidate.issue)
        candidates.append(candidate)

    collisions: set[str] = set()
    by_name: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        by_name.setdefault(portable_name_key(candidate.path.name), []).append(candidate)
    for key, matches in by_name.items():
        if len(matches) > 1:
            collisions.add(key)

    entries: list[AlbumIdentificationCatalogEntry] = []
    for candidate in candidates:
        if portable_name_key(candidate.path.name) in collisions:
            entries.append(
                _entry(
                    candidate,
                    status="invalid",
                    issues=(
                        IdentificationCatalogIssue(
                            "portable_name_collision",
                            "Portable-equivalent proposal filenames are ambiguous.",
                        ),
                    ),
                )
            )
            continue
        if candidate.loaded is None or candidate.issue is not None:
            entries.append(_entry(candidate, status="invalid"))
            continue
        proposal = candidate.loaded.proposal
        if _proposal_album_reference(proposal) != canonical.name:
            continue
        issues = _current_issues(
            proposal,
            album_name=canonical.name,
            context=context,
            runtime=runtime,
            context_error=context_error,
        )
        if not complete:
            issues.append(
                IdentificationCatalogIssue(
                    "catalog_scan_incomplete",
                    "Incomplete discovery prevents this proposal from being selectable.",
                )
            )
        try:
            repeated = candidate.path.lstat()
        except OSError:
            issues.append(
                IdentificationCatalogIssue(
                    "proposal_changed_during_discovery",
                    "The proposal changed during discovery.",
                )
            )
            entries.append(_entry(candidate, status="invalid", issues=tuple(issues)))
            continue
        if _file_identity(repeated) != candidate.loaded.identity:
            entries.append(
                _entry(
                    candidate,
                    status="invalid",
                    issues=(
                        IdentificationCatalogIssue(
                            "proposal_changed_during_discovery",
                            "The proposal changed during discovery.",
                        ),
                    ),
                )
            )
            continue
        entries.append(
            _entry(
                candidate,
                status="current" if not issues else "stale",
                issues=tuple(issues),
            )
        )

    _repeated_album, repeated_album_sha256 = load_album_project_with_sha256(canonical)
    if repeated_album_sha256 != album_sha256:
        raise ProjectValidationError(
            "The album project changed during identification-proposal discovery."
        )
    repeated_runtime = _runtime_identity(config)
    if repeated_runtime != runtime:
        raise ProjectValidationError(
            "The identification runtime changed during proposal discovery."
        )
    if context is not None:
        try:
            repeated_context = capture_album_identification_context(canonical)
        except ProjectValidationError as exc:
            raise ProjectValidationError(
                "The album context changed during proposal discovery."
            ) from exc
        if repeated_context.sha256 != context.sha256:
            raise ProjectValidationError(
                "The album context changed during proposal discovery."
            )
    return AlbumIdentificationProposalCatalog(
        album_reference=canonical.name,
        album_sha256=album_sha256,
        live_context_available=context is not None,
        scan_complete=complete,
        entries=tuple(entries),
        issues=tuple(catalog_issues),
    )


def load_current_album_identification_proposal(
    album_path: Path,
    proposal_path: Path,
    *,
    expected_file_sha256: str | None = None,
    config: AlbumIdentificationConfig | None = None,
) -> LoadedAlbumIdentificationProposal:
    """Reload one selectable proposal and re-prove every live binding."""

    canonical = canonical_album_path(album_path)
    supplied = Path(os.path.abspath(os.fspath(proposal_path.expanduser())))
    _assert_direct_sibling(canonical, supplied)
    loaded = load_album_identification_proposal_file(supplied)
    expected_path = album_identification_proposal_path(canonical, loaded.proposal)
    if supplied != expected_path:
        raise ProjectValidationError(
            "Identification proposal path does not match its immutable identity."
        )
    if expected_file_sha256 is not None:
        if (
            not isinstance(expected_file_sha256, str)
            or _DIGEST_RE.fullmatch(expected_file_sha256) is None
        ):
            raise ProjectValidationError("Expected proposal file SHA-256 is invalid.")
        if loaded.file_sha256 != expected_file_sha256:
            raise ProjectValidationError(
                "Identification proposal file changed after catalog discovery."
            )
    context = capture_album_identification_context(canonical)
    runtime = _runtime_identity(config)
    issues = _current_issues(
        loaded.proposal,
        album_name=canonical.name,
        context=context,
        runtime=runtime,
        context_error=None,
    )
    if issues:
        raise ProjectValidationError(
            "Identification proposal is not current and cannot be selected: "
            + "; ".join(issue.code for issue in issues)
        )
    repeated = load_album_identification_proposal_file(supplied)
    if repeated.file_sha256 != loaded.file_sha256 or repeated.proposal != loaded.proposal:
        raise ProjectValidationError(
            "Identification proposal changed during current-selection verification."
        )
    final_context = capture_album_identification_context(canonical)
    final_runtime = _runtime_identity(config)
    if final_context.sha256 != context.sha256 or final_runtime != runtime:
        raise ProjectValidationError(
            "Album or identification runtime changed during proposal selection."
        )
    return repeated


__all__ = [
    "ALBUM_IDENTIFICATION_CATALOG_SCHEMA",
    "PROPOSAL_FILENAME_PREFIX",
    "PROPOSAL_FILENAME_SUFFIX",
    "AlbumIdentificationCatalogEntry",
    "AlbumIdentificationProposalCatalog",
    "IdentificationCatalogIssue",
    "LoadedAlbumIdentificationProposal",
    "album_identification_proposal_path",
    "discover_album_identification_proposal_catalog",
    "load_album_identification_proposal_file",
    "load_current_album_identification_proposal",
    "save_album_identification_proposal",
]
