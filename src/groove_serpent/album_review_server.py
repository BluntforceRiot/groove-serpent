"""Loopback-only review server for the multi-side Album Workbench."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import http.client
import json
import os
import socket
import stat
import sys
import threading
import unicodedata
import uuid
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable, Mapping, cast
from urllib.parse import urlsplit

from .album import (
    MAX_ALBUM_METADATA_ITEMS,
    MAX_ALBUM_METADATA_KEY_LENGTH,
    MAX_ALBUM_METADATA_VALUE_LENGTH,
    MAX_ALBUM_REFERENCE_LENGTH,
    MAX_ALBUM_REVISION,
    AlbumSide,
    AlbumSpeed,
    AlbumProject,
    artwork_for_album_path,
    canonical_album_path,
    load_album_project,
    load_album_project_with_sha256,
    project_speed_state,
    repin_album_sides,
    resolve_album_reference,
    save_album_project,
)
from .album_publication_builder import build_album_publication_plan
from .album_publication_catalog import PUBLICATION_PLAN_FILENAME_SUFFIX
from .album_publication_durability import (
    RecoveryDirectoryIdentity,
    recover_album_publication_orphan,
    replay_album_publication,
    verify_album_publication,
)
from .album_publication_executor import (
    execute_album_publication_plan,
    preflight_album_publication_plan,
)
from .album_publication_plan import (
    PROFILE_ARCHIVAL_SOURCE,
    PROFILE_CORRECTED_LOSSLESS,
    PROFILE_PORTABLE,
    PROFILE_RESTORED_SIDE,
    load_album_publication_plan_with_sha256,
)
from .album_identification import (
    MAX_ALBUM_TRACKS,
    MAX_MATCHES_PER_TRACK,
    MAX_TOTAL_MATCHES,
    AlbumIdentificationContext,
    TrackRecognitionEvidence,
    capture_album_identification_context,
    propose_album_release_identification,
)
from .album_identification_catalog import (
    PROPOSAL_FILENAME_RE,
    album_identification_proposal_path,
    load_current_album_identification_proposal,
    save_album_identification_proposal,
)
from .album_workbench import build_album_workbench_state
from .audio_snapshot import VerifiedAudioSnapshot, verified_audio_snapshot
from .errors import ExportError, GrooveSerpentError, ProjectValidationError
from .media import sha256_file
from .metadata import (
    CoverArtArchiveClient,
    MetadataLookupError,
    MusicBrainzClient,
)
from .models import Project, resolve_source_path
from .portable_names import portable_name_key
from .project_io import load_project_with_sha256
from .publication import canonical_json_sha256
from .recognition import (
    AcoustIDRecognitionProvider,
    RecognitionError,
    RecognitionProvider,
)
from .review_server import ReviewServer
from .session_auth import (
    LoopbackSessionAuth,
    SessionAuthentication,
    request_target_is_exact,
)


_MAX_REQUEST_BODY = 64 * 1024
_MAX_ALBUM_SIDES = 64
_MAX_PUBLICATION_PROGRESS_MESSAGES = 64
_MAX_PUBLICATION_SIBLINGS = 4_096
_MAX_IDENTIFICATION_PROPOSALS = 64
_MAX_RELEASE_REVIEW_TRACKS = 2_048
_MAX_RELEASE_REVIEW_TEXT = 1_024
_ALBUM_RELEASE_REVIEW_SCHEMA = "groove-serpent.album-release-review/1"
_ALBUM_ARTWORK_REVIEW_SCHEMA = "groove-serpent.album-artwork-review/1"
_REPARSE_POINT = 0x400
_WINDOWS_DEVICE_STEMS = frozenset(
    {
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
)
_PUBLICATION_PROFILES = frozenset(
    {
        PROFILE_ARCHIVAL_SOURCE,
        PROFILE_RESTORED_SIDE,
        PROFILE_CORRECTED_LOSSLESS,
        PROFILE_PORTABLE,
    }
)
_EXPECTED_IDENTITY_KEYS = frozenset(
    {
        "project_revision",
        "project_sha256",
        "editable_state_sha256",
        "source_sha256",
        "project_speed_state_sha256",
    }
)
_STATIC_ROUTES = {
    "/": ("album.html", "text/html; charset=utf-8"),
    "/album.js": ("album.js", "text/javascript; charset=utf-8"),
    "/album.css": ("album.css", "text/css; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class _AlbumConflictError(GrooveSerpentError):
    """The album or a constituent side changed after browser review."""


@dataclass(slots=True)
class _SideReviewChild:
    """One exact side-review server owned by an Album Workbench session."""

    side_label: str
    project_path: Path
    current_identity: dict[str, int | str]
    server: ReviewServer
    thread: threading.Thread
    url: str = field(repr=False)

    def close(self) -> None:
        """Stop the listener and release its immutable source snapshot."""

        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)
        if self.thread.is_alive():
            raise RuntimeError(
                f"Side {self.side_label} review server did not stop cleanly."
            )


@dataclass(frozen=True, slots=True)
class _AlbumArtworkPreview:
    """One session-local preview bound to exact reviewed identities."""

    path: Path
    relative_path: str
    sha256: str
    size_bytes: int
    mime_type: str
    album_sha256: str
    album_revision: int
    proposal_filename: str
    proposal_file_sha256: str
    proposal_sha256: str
    release_mbid: str
    release_sha256: str


def _loopback_addresses(host: str) -> list[tuple[int, str]]:
    """Resolve a host only when every usable result is a loopback address."""

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host.casefold() != "localhost":
            return []
        try:
            resolved = socket.getaddrinfo(
                host,
                None,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        except OSError:
            return []
        addresses: list[tuple[int, str]] = []
        for family, _kind, _protocol, _canonical_name, sockaddr in resolved:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                return []
            try:
                resolved_address = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                return []
            if not resolved_address.is_loopback:
                return []
            item = (family, str(resolved_address))
            if item not in addresses:
                addresses.append(item)
        return addresses
    if not address.is_loopback:
        return []
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    return [(family, str(address))]


def _normalized_host(host: str) -> str:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return host.casefold()


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _strict_object(
    payload: Mapping[str, Any],
    *,
    fields: frozenset[str],
    label: str,
) -> None:
    actual = set(payload)
    if actual != fields:
        unknown = sorted(actual - fields)
        missing = sorted(fields - actual)
        details: list[str] = []
        if unknown:
            details.append("unsupported fields: " + ", ".join(unknown))
        if missing:
            details.append("missing fields: " + ", ".join(missing))
        raise ProjectValidationError(f"{label} has " + "; ".join(details) + ".")


def _strict_digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProjectValidationError(
            f"{label} must be 64 lowercase hexadecimal characters."
        )
    return value


def _strict_album_revision(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_ALBUM_REVISION:
        raise ProjectValidationError(
            "Expected album revision must be a bounded positive JSON integer."
        )
    return value


def _expected_identity(value: Any) -> dict[str, int | str]:
    if type(value) is not dict:
        raise ProjectValidationError("Expected current identity must be a JSON object.")
    payload = cast(dict[str, Any], value)
    _strict_object(
        payload,
        fields=_EXPECTED_IDENTITY_KEYS,
        label="Expected current identity",
    )
    revision = payload["project_revision"]
    if type(revision) is not int or revision < 1:
        raise ProjectValidationError(
            "Expected project revision must be a positive JSON integer."
        )
    return {
        "project_revision": revision,
        "project_sha256": _strict_digest(
            payload["project_sha256"], "Expected project SHA-256"
        ),
        "editable_state_sha256": _strict_digest(
            payload["editable_state_sha256"], "Expected editable-state SHA-256"
        ),
        "source_sha256": _strict_digest(
            payload["source_sha256"], "Expected source SHA-256"
        ),
        "project_speed_state_sha256": _strict_digest(
            payload["project_speed_state_sha256"],
            "Expected project speed-state SHA-256",
        ),
    }


def _expected_sides(value: Any) -> list[tuple[str, dict[str, int | str]]]:
    if type(value) is not list or not 1 <= len(value) <= _MAX_ALBUM_SIDES:
        raise ProjectValidationError(
            "Expected sides must be a non-empty bounded JSON array."
        )
    expected: list[tuple[str, dict[str, int | str]]] = []
    seen: set[str] = set()
    for raw_item in value:
        if type(raw_item) is not dict:
            raise ProjectValidationError("Each expected side must be a JSON object.")
        item = cast(dict[str, Any], raw_item)
        _strict_object(
            item,
            fields=frozenset({"side_label", "current_identity"}),
            label="Expected side",
        )
        side_label = _strict_side_label(item["side_label"])
        folded = portable_name_key(side_label)
        if folded in seen:
            raise ProjectValidationError(
                "Expected sides cannot contain duplicate portable-equivalent labels."
            )
        seen.add(folded)
        expected.append((side_label, _expected_identity(item["current_identity"])))
    return expected


def _strict_side_label(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 32
        or any(ord(character) < 32 for character in value)
    ):
        raise ProjectValidationError(
            "Side label must be 1-32 characters of trimmed printable text."
        )
    return value


def _strict_relative_reference(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_ALBUM_REFERENCE_LENGTH
        or any(ord(character) < 32 for character in value)
    ):
        raise ProjectValidationError(
            f"{label} must be bounded, trimmed, printable text."
        )
    candidate = Path(value)
    if (
        candidate.is_absolute()
        or candidate.drive
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ProjectValidationError(
            f"{label} must be a relative path contained by the album-project folder."
        )
    return candidate.as_posix()


def _strict_publication_plan_filename(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 255
        or Path(value).name != value
        or "/" in value
        or "\\" in value
        or not value.endswith(PUBLICATION_PLAN_FILENAME_SUFFIX)
        or any(ord(character) < 32 for character in value)
    ):
        raise ProjectValidationError(
            "Publication-plan filename must be one bounded filename ending "
            f"with {PUBLICATION_PLAN_FILENAME_SUFFIX!r}."
        )
    return value


def _strict_catalog_plan_filename(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 255
        or Path(value).name != value
        or "/" in value
        or "\\" in value
        or Path(value).suffix.casefold() != ".json"
        or any(ord(character) < 32 for character in value)
    ):
        raise RuntimeError("Current publication catalog entry has an unsafe filename.")
    return value


def _strict_publication_destination_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 180
        or Path(value).name != value
        or Path(value).is_absolute()
        or value.startswith(".")
        or unicodedata.normalize("NFC", value) != value
        or any(ord(character) < 32 for character in value)
        or any(character in '<>:"/\\|?*' for character in value)
        or value.endswith((" ", "."))
        or value.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_STEMS
    ):
        raise ProjectValidationError(
            "Publication destination must be one canonical portable directory name."
        )
    return value


def _strict_orphan_directory_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or Path(value).name != value
        or "/" in value
        or "\\" in value
        or not value.startswith(
            (
                ".groove-serpent-album-publication-",
                ".groove-serpent-album-cleanup-",
            )
        )
        or not value.endswith(".partial")
        or any(ord(character) < 32 for character in value)
    ):
        raise ProjectValidationError(
            "Publication recovery requires one recognized direct-child orphan name."
        )
    return value


def _strict_recovery_identity(value: Any) -> RecoveryDirectoryIdentity:
    fields = frozenset(
        {"device", "inode", "file_type", "birth_ns", "file_attributes"}
    )
    if type(value) is not dict:
        raise ProjectValidationError("Recovery directory identity must be an object.")
    payload = cast(dict[str, Any], value)
    _strict_object(payload, fields=fields, label="Recovery directory identity")
    integers: dict[str, int | None] = {}
    for name in fields:
        item = payload[name]
        if name in {"birth_ns", "file_attributes"} and item is None:
            integers[name] = None
            continue
        if (
            not isinstance(item, str)
            or not item.isascii()
            or not item.isdigit()
            or len(item) > 20
        ):
            raise ProjectValidationError(
                f"Recovery directory identity {name} must be an exact decimal string."
            )
        parsed = int(item)
        if parsed > (1 << 64) - 1:
            raise ProjectValidationError(
                f"Recovery directory identity {name} exceeds its bound."
            )
        integers[name] = parsed
    device = integers["device"]
    inode = integers["inode"]
    file_type = integers["file_type"]
    if device is None or inode is None or file_type is None:
        raise ProjectValidationError("Recovery directory identity is incomplete.")
    return RecoveryDirectoryIdentity(
        device=device,
        inode=inode,
        file_type=file_type,
        birth_ns=integers["birth_ns"],
        file_attributes=integers["file_attributes"],
    )


def _strict_publication_profiles(value: Any) -> tuple[str, ...]:
    if type(value) is not list or not 1 <= len(value) <= len(_PUBLICATION_PROFILES):
        raise ProjectValidationError(
            "Publication profiles must be a non-empty bounded JSON array."
        )
    if not all(isinstance(item, str) for item in value):
        raise ProjectValidationError("Publication profile names must be text.")
    profiles = tuple(cast(list[str], value))
    if len(set(profiles)) != len(profiles):
        raise ProjectValidationError("Publication profiles must not repeat.")
    unsupported = set(profiles) - _PUBLICATION_PROFILES
    if unsupported:
        rendered = ", ".join(repr(item) for item in sorted(unsupported))
        raise ProjectValidationError(
            f"Unsupported publication profile(s): {rendered}."
        )
    return profiles


def _strict_publication_integer(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProjectValidationError(
            f"{label} must be a JSON integer from {minimum} through {maximum}."
        )
    return value


def _strict_metadata(value: Any) -> dict[str, str]:
    if type(value) is not dict:
        raise ProjectValidationError("Album metadata must be a JSON object.")
    payload = value
    if len(payload) > MAX_ALBUM_METADATA_ITEMS:
        raise ProjectValidationError(
            f"Album metadata cannot exceed {MAX_ALBUM_METADATA_ITEMS} entries."
        )
    metadata: dict[str, str] = {}
    for key, item in payload.items():
        if (
            not isinstance(key, str)
            or not key
            or key != key.strip()
            or len(key) > MAX_ALBUM_METADATA_KEY_LENGTH
            or not isinstance(item, str)
            or len(item) > MAX_ALBUM_METADATA_VALUE_LENGTH
        ):
            raise ProjectValidationError(
                "Album metadata keys and values must be bounded text, and keys "
                "must be non-empty and trimmed."
            )
        metadata[key] = item
    return metadata


def _is_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & flag)


def _plain_project_source(project: Project, project_path: Path) -> Path:
    stored = Path(project.source.path)
    candidates = [
        stored if stored.is_absolute() else project_path.parent / stored,
        project_path.parent / project.source.filename,
    ]
    for supplied in candidates:
        absolute = Path(os.path.abspath(os.fspath(supplied.expanduser())))
        try:
            candidate = absolute.parent.resolve(strict=True) / absolute.name
            value = candidate.lstat()
        except (OSError, RuntimeError):
            continue
        if candidate.is_symlink() or _is_reparse(value):
            raise ProjectValidationError(
                "A side project's source must not be a final symlink or reparse point."
            )
        if stat.S_ISREG(value.st_mode):
            return candidate
    raise ProjectValidationError(
        "The side project's source audio is not available as a regular file."
    )


def _new_unpinned_side(
    album_path: Path,
    *,
    label: str,
    project_reference: str,
    order: int,
) -> AlbumSide:
    side = AlbumSide(label=label, order=order, project=project_reference, pin=None)
    side.validate()
    project_path = resolve_album_reference(
        album_path,
        side.project,
        "Album side project reference",
    )
    try:
        project, project_sha256 = load_project_with_sha256(project_path)
    except OSError as exc:
        raise ProjectValidationError(
            "The album side project does not exist or cannot be read as a regular file."
        ) from exc
    source_path = _plain_project_source(project, project_path)
    actual_source_sha256 = sha256_file(source_path).lower()
    if (
        not project.source.sha256
        or actual_source_sha256 != project.source.sha256.lower()
    ):
        raise ProjectValidationError(
            f"Side {label} source no longer matches its current project SHA-256."
        )
    repeated_project, repeated_sha256 = load_project_with_sha256(project_path)
    if (
        repeated_sha256 != project_sha256
        or repeated_project.revision != project.revision
        or repeated_project.state_sha256 != project.state_sha256
        or sha256_file(source_path).lower() != actual_source_sha256
    ):
        raise _AlbumConflictError(
            f"Side {label} project or source changed while it was being added. Reload."
        )
    speed = project_speed_state(project)
    side.speed = AlbumSpeed.create("inherit", speed, speed)
    side.pin = None
    side.validate()
    return side


def _side_state(state: Mapping[str, Any], side_label: str) -> dict[str, Any]:
    sides = state.get("sides")
    if type(sides) is not list:
        raise RuntimeError("Album Workbench state has no side list.")
    for raw_side in sides:
        if type(raw_side) is not dict:
            raise RuntimeError("Album Workbench state contains an invalid side.")
        side = cast(dict[str, Any], raw_side)
        if side.get("label") == side_label:
            return side
    raise ProjectValidationError(f"Unknown album side label: {side_label!r}.")


def _side_current_identity(
    state: Mapping[str, Any], side_label: str
) -> dict[str, int | str]:
    identity = _side_state(state, side_label).get("current_identity")
    if type(identity) is not dict or set(identity) != _EXPECTED_IDENTITY_KEYS:
        raise RuntimeError("Album Workbench state contains an invalid side identity.")
    try:
        return _expected_identity(identity)
    except ProjectValidationError as exc:
        raise RuntimeError(
            "Album Workbench state contains an invalid side identity."
        ) from exc


def _resolved_side_project(state: Mapping[str, Any], side_label: str) -> Path:
    rendered = _side_state(state, side_label).get("resolved_project")
    if not isinstance(rendered, str) or not rendered:
        raise RuntimeError("Album Workbench state has no resolved side project.")
    candidate = Path(rendered)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _AlbumConflictError(
            f"Side {side_label} project is no longer available. Reload."
        ) from exc
    if candidate != resolved or not resolved.is_file():
        raise _AlbumConflictError(
            f"Side {side_label} project changed after it was loaded. Reload."
        )
    return resolved


def _review_server_url(server: ReviewServer) -> str:
    address = server.server_address
    if server.address_family != socket.AF_INET or not isinstance(address, tuple):
        raise RuntimeError("Side review server did not bind an IPv4 loopback endpoint.")
    if len(address) != 2:
        raise RuntimeError("Side review server returned an invalid endpoint.")
    host, port = address
    if not isinstance(host, str) or type(port) is not int:
        raise RuntimeError("Side review server returned an invalid endpoint.")
    try:
        address_value = ipaddress.ip_address(host)
    except ValueError as exc:
        raise RuntimeError("Side review server returned an invalid address.") from exc
    if not address_value.is_loopback or port <= 0:
        raise RuntimeError("Side review server did not bind to loopback.")
    return server.session_auth.bootstrap_url(port=port)


def _workbench_state(
    album: AlbumProject,
    album_path: Path,
    album_sha256: str,
    recognition_provider: RecognitionProvider | None = None,
) -> dict[str, Any]:
    readiness = (
        None if recognition_provider is None else recognition_provider.readiness()
    )
    state = build_album_workbench_state(
        album,
        album_path,
        recognition_readiness=readiness,
    )
    if not isinstance(state, dict):
        raise RuntimeError("Album Workbench returned an invalid state.")
    if state.get("album_project_sha256") != album_sha256:
        raise _AlbumConflictError(
            "The album project changed while its review state was being prepared. Reload."
        )
    return state


def _assert_fixed_album_destination(album_path: Path) -> None:
    """Reject a replaced or redirected album path before an atomic save."""

    try:
        if album_path.resolve(strict=True) != album_path or not album_path.is_file():
            raise _AlbumConflictError(
                "The album-project destination changed after the server started. Reload."
            )
    except (OSError, RuntimeError) as exc:
        raise _AlbumConflictError(
            "The album-project destination is no longer safe to replace. Reload."
        ) from exc


def _album_digest_or_conflict(album_path: Path) -> str:
    try:
        return sha256_file(album_path)
    except OSError as exc:
        raise _AlbumConflictError(
            "The album project is no longer available. Reload."
        ) from exc


def _state_side_identities(
    state: Mapping[str, Any],
) -> list[tuple[str, dict[str, int | str]]]:
    raw_sides = state.get("sides")
    if type(raw_sides) is not list:
        raise RuntimeError("Album Workbench state has no side list.")
    identities: list[tuple[str, dict[str, int | str]]] = []
    for raw_side in raw_sides:
        if type(raw_side) is not dict:
            raise RuntimeError("Album Workbench state contains an invalid side.")
        side = cast(dict[str, Any], raw_side)
        label = side.get("label")
        if not isinstance(label, str):
            raise RuntimeError("Album Workbench state contains an invalid side label.")
        identities.append((label, _side_current_identity(state, label)))
    return identities


def _assert_expected_sides(
    state: Mapping[str, Any],
    expected: list[tuple[str, dict[str, int | str]]],
) -> None:
    if _state_side_identities(state) != expected:
        raise _AlbumConflictError(
            "An album side changed after this review page loaded. Reload."
        )


def _assert_no_stale_sources(state: Mapping[str, Any]) -> None:
    raw_exceptions = state.get("exceptions")
    if type(raw_exceptions) is not list:
        raise RuntimeError("Album Workbench state has no exception list.")
    for raw_exception in raw_exceptions:
        if (
            type(raw_exception) is dict
            and raw_exception.get("type") == "source_project_mismatch"
        ):
            raise _AlbumConflictError(
                "A side source no longer matches its project. Reload after repairing "
                "the stale source reference."
            )


def _identification_state(state: Mapping[str, Any]) -> dict[str, Any]:
    value = state.get("identification")
    if type(value) is not dict:
        raise RuntimeError("Album state contains an invalid identification section.")
    return cast(dict[str, Any], value)


def _identification_catalog_entry(
    state: Mapping[str, Any],
    *,
    filename: str,
    proposal_sha256: str,
    file_sha256: str | None = None,
) -> dict[str, Any]:
    identification = _identification_state(state)
    catalog = identification.get("catalog")
    if type(catalog) is not dict:
        raise RuntimeError("Album state contains an invalid identification catalog.")
    entries = cast(dict[str, Any], catalog).get("entries")
    if (
        not isinstance(entries, list)
        or len(entries) > _MAX_IDENTIFICATION_PROPOSALS
    ):
        raise RuntimeError("Album state contains invalid identification entries.")
    matches = [
        item
        for item in entries
        if type(item) is dict
        and item.get("filename") == filename
        and item.get("proposal_sha256") == proposal_sha256
        and (file_sha256 is None or item.get("file_sha256") == file_sha256)
    ]
    if len(matches) != 1 or matches[0].get("status") != "current":
        raise _AlbumConflictError(
            "The identification proposal is no longer the exact current proposal. Reload."
        )
    return cast(dict[str, Any], matches[0])


def _strict_identification_proposal_filename(value: Any) -> str:
    if not isinstance(value, str) or PROPOSAL_FILENAME_RE.fullmatch(value) is None:
        raise ProjectValidationError(
            "Identification proposal filename is not a canonical proposal name."
        )
    return value


def _strict_release_mbid(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ProjectValidationError("Release MBID must be a canonical UUID.")
    try:
        normalized = str(uuid.UUID(value))
    except (AttributeError, ValueError) as exc:
        raise ProjectValidationError("Release MBID must be a canonical UUID.") from exc
    if normalized != value.casefold():
        raise ProjectValidationError("Release MBID must be a canonical UUID.")
    return normalized


def _review_text(value: Any, label: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ProjectValidationError(f"{label} must be text.")
    rendered = value.strip()
    if (not allow_empty and not rendered) or len(rendered) > _MAX_RELEASE_REVIEW_TEXT:
        raise ProjectValidationError(f"{label} is missing or too long.")
    if any(ord(character) < 32 and character not in "\t\n" for character in rendered):
        raise ProjectValidationError(f"{label} contains unsupported control characters.")
    return rendered


def _review_text_list(value: Any, label: str, *, maximum: int = 64) -> list[str]:
    if type(value) is not list or len(value) > maximum:
        raise ProjectValidationError(f"{label} must be a bounded JSON array.")
    normalized: list[str] = []
    for item in value:
        rendered = _review_text(item, label)
        if rendered and rendered not in normalized:
            normalized.append(rendered)
    return normalized


def _normalize_release_review(value: Any, release_mbid: str) -> dict[str, Any]:
    """Reduce MusicBrainz details to one bounded, strict review document."""

    if type(value) is not dict:
        raise ProjectValidationError("MusicBrainz returned invalid release details.")
    release = cast(dict[str, Any], value)
    if _strict_release_mbid(release.get("id")) != release_mbid:
        raise ProjectValidationError("MusicBrainz returned a different release identity.")
    release_group_id = _review_text(
        release.get("release_group_id", ""), "Release-group MBID"
    )
    if release_group_id:
        _strict_release_mbid(release_group_id)
    raw_track_count = release.get("track_count")
    if (
        type(raw_track_count) is not int
        or not 0 <= raw_track_count <= _MAX_RELEASE_REVIEW_TRACKS
    ):
        raise ProjectValidationError("MusicBrainz returned an invalid release track count.")
    has_artwork = release.get("has_artwork")
    if type(has_artwork) is not bool:
        raise ProjectValidationError("MusicBrainz returned invalid artwork availability.")

    raw_media = release.get("media")
    if type(raw_media) is not list or len(raw_media) > 64:
        raise ProjectValidationError("MusicBrainz returned invalid release media.")
    tracklist: list[dict[str, Any]] = []
    media: list[dict[str, Any]] = []
    for fallback_position, raw_medium in enumerate(raw_media, start=1):
        if type(raw_medium) is not dict:
            raise ProjectValidationError("MusicBrainz returned invalid release media.")
        medium = cast(dict[str, Any], raw_medium)
        position = medium.get("position", fallback_position)
        medium_track_count = medium.get("track_count")
        tracks = medium.get("tracks")
        if (
            type(position) is not int
            or position < 1
            or type(medium_track_count) is not int
            or medium_track_count < 0
            or type(tracks) is not list
            or len(tracks) != medium_track_count
        ):
            raise ProjectValidationError("MusicBrainz returned inconsistent release media.")
        medium_title = _review_text(medium.get("title", ""), "Medium title")
        medium_format = _review_text(medium.get("format", ""), "Medium format")
        media.append(
            {
                "position": position,
                "title": medium_title,
                "format": medium_format,
                "track_count": medium_track_count,
            }
        )
        for fallback_track, raw_track in enumerate(tracks, start=1):
            if type(raw_track) is not dict:
                raise ProjectValidationError("MusicBrainz returned an invalid track list.")
            track = cast(dict[str, Any], raw_track)
            track_position = track.get("position", fallback_track)
            duration = track.get("duration_seconds")
            if type(track_position) is not int or track_position < 1:
                raise ProjectValidationError("MusicBrainz returned an invalid track position.")
            if duration is not None and (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not 0 <= float(duration) <= 86_400
            ):
                raise ProjectValidationError("MusicBrainz returned an invalid track duration.")
            tracklist.append(
                {
                    "medium_position": position,
                    "medium_title": medium_title,
                    "medium_format": medium_format,
                    "position": track_position,
                    "number": _review_text(track.get("number", ""), "Track number"),
                    "title": _review_text(
                        track.get("title", ""), "Track title", allow_empty=False
                    ),
                    "artist": _review_text(track.get("artist", ""), "Track artist"),
                    "duration_seconds": (
                        None if duration is None else round(float(duration), 3)
                    ),
                }
            )
            if len(tracklist) > _MAX_RELEASE_REVIEW_TRACKS:
                raise ProjectValidationError("MusicBrainz release track list is too large.")
    if len(tracklist) != raw_track_count:
        raise ProjectValidationError("MusicBrainz returned an inconsistent track count.")

    return {
        "release_mbid": release_mbid,
        "title": _review_text(
            release.get("title", ""), "Release title", allow_empty=False
        ),
        "artist": _review_text(release.get("artist", ""), "Release artist"),
        "date": _review_text(release.get("date", ""), "Release date"),
        "country": _review_text(release.get("country", ""), "Release country"),
        "status": _review_text(release.get("status", ""), "Release status"),
        "barcode": _review_text(release.get("barcode", ""), "Release barcode"),
        "label": _review_text(release.get("label", ""), "Release label"),
        "catalog_number": _review_text(
            release.get("catalog_number", ""), "Release catalog number"
        ),
        "release_group_mbid": release_group_id,
        "genres": _review_text_list(release.get("genres", []), "Release genres"),
        "formats": _review_text_list(release.get("formats", []), "Release formats"),
        "track_count": raw_track_count,
        "has_artwork": has_artwork,
        "media": media,
        "tracklist": tracklist,
    }


def _release_review_authority(*, artwork_downloaded: bool) -> dict[str, bool]:
    return {
        "read_only": True,
        "metadata_applied": False,
        "artwork_downloaded": artwork_downloaded,
        "artwork_applied": False,
        "may_modify_album_project": False,
        "may_modify_side_projects": False,
        "physical_pressing_proven": False,
        "human_review_required": True,
    }


def _current_identification_candidate(
    album_path: Path,
    state: Mapping[str, Any],
    *,
    filename: str,
    file_sha256: str,
    proposal_sha256: str,
    release_mbid: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    entry = _identification_catalog_entry(
        state,
        filename=filename,
        proposal_sha256=proposal_sha256,
        file_sha256=file_sha256,
    )
    loaded = load_current_album_identification_proposal(
        album_path,
        album_path.parent / filename,
        expected_file_sha256=file_sha256,
    )
    proposal = loaded.proposal
    if proposal.get("proposal_sha256") != proposal_sha256:
        raise _AlbumConflictError("The identification proposal identity changed. Reload.")
    raw_candidates = proposal.get("ranked_release_candidates")
    if type(raw_candidates) is not list:
        raise RuntimeError("Identification proposal has no ranked candidate list.")
    candidates = [
        cast(dict[str, Any], item)
        for item in raw_candidates
        if type(item) is dict and item.get("release_mbid") == release_mbid
    ]
    if len(candidates) != 1:
        raise ProjectValidationError(
            "The selected MusicBrainz release is not an exact ranked candidate."
        )
    candidate = candidates[0]
    album_identity = proposal.get("album")
    if type(album_identity) is not dict:
        raise RuntimeError("Identification proposal has no album identity.")
    album = cast(dict[str, Any], album_identity)
    sides = album.get("sides")
    if type(sides) is not list:
        raise RuntimeError("Identification proposal has no source bindings.")
    binding = {
        "album_reference": album.get("album_reference"),
        "album_sha256": album.get("album_sha256"),
        "album_revision": album.get("album_revision"),
        "album_context_sha256": album.get("context_sha256"),
        "source_bindings_sha256": canonical_json_sha256(sides),
        "proposal_filename": filename,
        "proposal_file_sha256": file_sha256,
        "proposal_sha256": proposal_sha256,
        "candidate_sha256": canonical_json_sha256(candidate),
        "release_mbid": release_mbid,
    }
    return proposal, candidate, entry, binding


def _safe_review_artwork_path(
    album_path: Path,
    *,
    relative_path: str,
    expected_sha256: str,
    expected_size: int,
) -> tuple[Path, bytes]:
    """Open and verify one contained review image without trusting its pathname."""

    normalized = _strict_relative_reference(relative_path, "Review artwork path")
    if "\\" in normalized or not normalized.startswith("artwork/review/"):
        raise ProjectValidationError(
            "Downloaded review artwork must stay in artwork/review/."
        )
    root = album_path.parent.resolve(strict=True)
    candidate = root / Path(normalized)
    try:
        if candidate.parent.resolve(strict=True) != candidate.parent:
            raise ProjectValidationError("Review artwork path traverses a linked folder.")
        before = candidate.lstat()
        if (
            candidate.is_symlink()
            or _is_reparse(before)
            or not stat.S_ISREG(before.st_mode)
            or int(before.st_nlink) != 1
        ):
            raise ProjectValidationError(
                "Review artwork must be a single-link regular non-reparse file."
            )
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        with candidate.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not os.path.samestat(before, opened) or opened.st_size != expected_size:
                raise ProjectValidationError("Review artwork identity changed before preview.")
            digest = hashlib.sha256()
            body = bytearray()
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                body.extend(chunk)
            after = os.fstat(handle.fileno())
        current = candidate.lstat()
    except ProjectValidationError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectValidationError("Review artwork is no longer a safe local file.") from exc
    if (
        not os.path.samestat(before, after)
        or not os.path.samestat(before, current)
        or _is_reparse(current)
        or digest.hexdigest() != expected_sha256
    ):
        raise ProjectValidationError("Review artwork changed after download.")
    return candidate, bytes(body)


def _normalize_artwork_download(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {
        "relative_path",
        "source_url",
        "mime_type",
        "sha256",
        "size_bytes",
        "requested_size",
        "selected_size",
    }:
        raise ProjectValidationError("Cover Art Archive returned invalid download metadata.")
    artwork = cast(dict[str, Any], value)
    relative_path = _strict_relative_reference(
        artwork["relative_path"], "Review artwork path"
    )
    if "\\" in relative_path or not relative_path.startswith("artwork/review/"):
        raise ProjectValidationError(
            "Downloaded review artwork must stay in artwork/review/."
        )
    source_url = _review_text(artwork["source_url"], "Artwork source URL")
    parsed = urlsplit(source_url)
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not (
        hostname == "coverartarchive.org"
        or hostname.endswith(".coverartarchive.org")
        or hostname == "archive.org"
        or hostname.endswith(".archive.org")
    ):
        raise ProjectValidationError("Artwork source URL is not a trusted HTTPS origin.")
    mime_type = artwork["mime_type"]
    if mime_type not in {"image/jpeg", "image/png"}:
        raise ProjectValidationError("Artwork download has an unsupported image type.")
    sha256 = _strict_digest(artwork["sha256"], "Downloaded artwork SHA-256")
    size_bytes = artwork["size_bytes"]
    if type(size_bytes) is not int or not 1 <= size_bytes <= 25 * 1024 * 1024:
        raise ProjectValidationError("Artwork download has an invalid byte length.")
    requested_size = artwork["requested_size"]
    selected_size = artwork["selected_size"]
    if requested_size != "1200" or selected_size not in {"1200", "original"}:
        raise ProjectValidationError("Artwork download has an invalid size selection.")
    return {
        "relative_path": relative_path,
        "source_url": source_url,
        "mime_type": mime_type,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "requested_size": requested_size,
        "selected_size": selected_size,
    }


def _discard_exact_review_artwork(album_path: Path, artwork: Mapping[str, Any]) -> None:
    """Remove only the exact server-created file after a failed final identity lease."""

    try:
        candidate, _body = _safe_review_artwork_path(
            album_path,
            relative_path=cast(str, artwork["relative_path"]),
            expected_sha256=cast(str, artwork["sha256"]),
            expected_size=cast(int, artwork["size_bytes"]),
        )
        candidate.unlink()
    except (KeyError, OSError, ProjectValidationError, TypeError, ValueError):
        return


def _snapshot_album_identification_inputs(
    album_path: Path,
    context: AlbumIdentificationContext,
) -> tuple[
    list[tuple[str, Project, VerifiedAudioSnapshot]],
    list[VerifiedAudioSnapshot],
]:
    """Capture one immutable snapshot per unique current album source."""

    track_count = sum(len(side.tracks) for side in context.sides)
    if not 1 <= track_count <= MAX_ALBUM_TRACKS:
        raise ProjectValidationError(
            f"Album identification requires 1-{MAX_ALBUM_TRACKS} current tracks."
        )
    snapshots_by_path: dict[Path, VerifiedAudioSnapshot] = {}
    snapshots: list[VerifiedAudioSnapshot] = []
    inputs: list[tuple[str, Project, VerifiedAudioSnapshot]] = []
    try:
        for side in context.sides:
            project_path = resolve_album_reference(
                album_path,
                side.project_reference,
                f"Side {side.label} project",
            )
            project, project_sha256 = load_project_with_sha256(project_path)
            if (
                project_sha256 != side.project_sha256
                or project.revision != side.project_revision
                or project.state_sha256 != side.project_state_sha256
            ):
                raise _AlbumConflictError(
                    f"Side {side.label} changed before fingerprint capture. Reload."
                )
            source_path = resolve_source_path(project, project_path).resolve()
            expected_source_sha256 = str(project.source.sha256 or "").lower()
            if (
                expected_source_sha256 != side.source_sha256
                or project.source.size_bytes != side.source_size_bytes
            ):
                raise _AlbumConflictError(
                    f"Side {side.label} source binding changed before fingerprint capture."
                )
            snapshot = snapshots_by_path.get(source_path)
            if snapshot is None:
                snapshot = verified_audio_snapshot(
                    source_path,
                    expected_sha256=side.source_sha256,
                    expected_size_bytes=side.source_size_bytes,
                    label=f"Side {side.label} identification source audio",
                )
                snapshots_by_path[source_path] = snapshot
                snapshots.append(snapshot)
            elif (
                snapshot.sha256 != side.source_sha256
                or snapshot.size_bytes != side.source_size_bytes
            ):
                raise _AlbumConflictError(
                    "One shared album source has conflicting side identities. Reload."
                )
            inputs.append((side.label, project, snapshot))
        return inputs, snapshots
    except BaseException:
        for snapshot in reversed(snapshots):
            snapshot.close()
        raise


def _publication_catalog_entries(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    publication = state.get("publication")
    if type(publication) is not dict:
        raise RuntimeError("Album Workbench state has no publication state.")
    catalog = publication.get("catalog")
    if type(catalog) is not dict:
        raise RuntimeError("Album Workbench state has no publication catalog.")
    entries = catalog.get("entries")
    if type(entries) is not list or len(entries) > 128:
        raise RuntimeError("Album Workbench publication catalog is invalid.")
    result: list[dict[str, Any]] = []
    for entry in entries:
        if type(entry) is not dict:
            raise RuntimeError("Album Workbench publication catalog is invalid.")
        result.append(cast(dict[str, Any], entry))
    return result


def _current_publication_plan_entry(
    state: Mapping[str, Any],
    plan_sha256: str,
) -> dict[str, Any]:
    matches = [
        entry
        for entry in _publication_catalog_entries(state)
        if entry.get("status") == "current"
        and entry.get("plan_sha256") == plan_sha256
    ]
    if len(matches) != 1:
        raise _AlbumConflictError(
            "The selected publication plan is no longer current. Reload."
        )
    filename = matches[0].get("filename")
    if not isinstance(filename, str):
        raise RuntimeError("Current publication catalog entry has no filename.")
    _strict_catalog_plan_filename(filename)
    return matches[0]


def _publication_operations(state: Mapping[str, Any]) -> dict[str, Any]:
    publication = state.get("publication")
    if type(publication) is not dict:
        raise RuntimeError("Album Workbench state has no publication state.")
    operations = publication.get("operations")
    if type(operations) is not dict:
        raise RuntimeError("Album Workbench state has no publication operations.")
    return cast(dict[str, Any], operations)


def _publication_receipt_entries(
    state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    entries = _publication_operations(state).get("publications")
    if type(entries) is not list or len(entries) > 128:
        raise RuntimeError("Album Workbench publication receipts are invalid.")
    if any(type(entry) is not dict for entry in entries):
        raise RuntimeError("Album Workbench publication receipts are invalid.")
    return [cast(dict[str, Any], entry) for entry in entries]


def _publication_orphan_entries(
    state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    entries = _publication_operations(state).get("orphans")
    if type(entries) is not list or len(entries) > 256:
        raise RuntimeError("Album Workbench publication orphans are invalid.")
    if any(type(entry) is not dict for entry in entries):
        raise RuntimeError("Album Workbench publication orphans are invalid.")
    return [cast(dict[str, Any], entry) for entry in entries]


def _publication_receipt_entry(
    state: Mapping[str, Any],
    *,
    directory_name: str,
    manifest_sha256: str,
    journal_sha256: str,
    plan_sha256: str,
    allowed_statuses: frozenset[str],
) -> dict[str, Any]:
    matches = [
        entry
        for entry in _publication_receipt_entries(state)
        if entry.get("directory_name") == directory_name
        and entry.get("manifest_sha256") == manifest_sha256
        and entry.get("journal_sha256") == journal_sha256
        and entry.get("plan_sha256") == plan_sha256
        and entry.get("status") in allowed_statuses
    ]
    if len(matches) != 1:
        raise _AlbumConflictError(
            "The selected publication receipt is no longer exact. Reload."
        )
    return matches[0]


def _publication_orphan_entry(
    state: Mapping[str, Any],
    *,
    directory_name: str,
    kind: str,
    plan_sha256: str,
    journal_sha256: str,
    identity: RecoveryDirectoryIdentity,
) -> dict[str, Any]:
    expected_identity = {
        "device": str(identity.device),
        "inode": str(identity.inode),
        "file_type": str(identity.file_type),
        "birth_ns": None if identity.birth_ns is None else str(identity.birth_ns),
        "file_attributes": (
            None
            if identity.file_attributes is None
            else str(identity.file_attributes)
        ),
    }
    matches = [
        entry
        for entry in _publication_orphan_entries(state)
        if entry.get("directory_name") == directory_name
        and entry.get("kind") == kind
        and entry.get("plan_sha256") == plan_sha256
        and entry.get("journal_sha256") == journal_sha256
        and entry.get("directory_identity") == expected_identity
        and entry.get("actionable") is True
        and entry.get("belongs_to_album") is True
    ]
    if len(matches) != 1:
        raise _AlbumConflictError(
            "The selected owned publication orphan is no longer exact. Reload."
        )
    return matches[0]


def _assert_new_publication_destination(
    album_path: Path,
    destination_name: str,
) -> Path:
    parent = album_path.parent
    try:
        before = parent.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or _is_reparse(before)
            or not stat.S_ISDIR(before.st_mode)
            or parent.resolve(strict=True) != parent
        ):
            raise ProjectValidationError(
                "The album folder is not a fixed non-reparse destination."
            )
        target_key = portable_name_key(destination_name)
        with os.scandir(parent) as entries:
            for count, entry in enumerate(entries, start=1):
                if count > _MAX_PUBLICATION_SIBLINGS:
                    raise ProjectValidationError(
                        "The album folder has too many siblings to prove a new "
                        "portable destination."
                    )
                if portable_name_key(entry.name) == target_key:
                    raise ProjectValidationError(
                        "Publication destination or a portable-equivalent sibling "
                        f"already exists: {entry.name}."
                    )
        after = parent.lstat()
    except ProjectValidationError:
        raise
    except OSError as exc:
        raise ProjectValidationError(
            "The album folder could not prove a safe new publication destination."
        ) from exc
    if not os.path.samestat(before, after) or _is_reparse(after):
        raise _AlbumConflictError(
            "The album folder changed while the destination was checked. Reload."
        )
    return parent / destination_name


def _progress_capture() -> tuple[list[str], Callable[[str], None]]:
    messages: list[str] = []

    def capture(message: str) -> None:
        if not isinstance(message, str):
            return
        bounded = message.strip()[:512]
        if bounded and len(messages) < _MAX_PUBLICATION_PROGRESS_MESSAGES:
            messages.append(bounded)

    return messages, capture


def _verification_payload(report: Any) -> dict[str, Any]:
    return {
        "publication_directory": Path(report.publication_directory).name,
        "ok": report.ok,
        "manifest_sha256": report.manifest_sha256,
        "journal_sha256": report.journal_sha256,
        "artifact_count": report.artifact_count,
        "mismatches": [
            {
                "code": mismatch.code,
                "path": mismatch.path,
                "expected": mismatch.expected,
                "current": mismatch.current,
                "message": mismatch.message,
            }
            for mismatch in report.mismatches
        ],
    }


def _load_expected_album(
    album_path: Path,
    *,
    expected_sha256: str,
    expected_revision: int,
    recognition_provider: RecognitionProvider | None = None,
) -> tuple[AlbumProject, dict[str, Any]]:
    _assert_fixed_album_destination(album_path)
    if _album_digest_or_conflict(album_path) != expected_sha256:
        raise _AlbumConflictError(
            "The album project changed after this review page loaded. Reload."
        )
    try:
        album, album_sha256 = load_album_project_with_sha256(album_path)
        state = _workbench_state(
            album,
            album_path,
            album_sha256,
            recognition_provider,
        )
    except ProjectValidationError as exc:
        raise _AlbumConflictError(
            "The album project or one of its references changed. Reload."
        ) from exc
    if album_sha256 != expected_sha256 or album.revision != expected_revision:
        raise _AlbumConflictError(
            "The album project changed after this review page loaded. Reload."
        )
    if _album_digest_or_conflict(album_path) != expected_sha256:
        raise _AlbumConflictError(
            "The album project changed while its state was loaded. Reload."
        )
    return album, state


def _save_album_mutation(
    server: "AlbumReviewServer",
    album: AlbumProject,
    *,
    expected_sha256: str,
    expected_revision: int,
    expected_sides: list[tuple[str, dict[str, int | str]]],
) -> dict[str, Any]:
    album.validate()
    proposed_state = build_album_workbench_state(
        album,
        server.album_path,
        recognition_readiness=server.recognition_provider.readiness(),
    )
    _assert_no_stale_sources(proposed_state)
    proposed_identities = _state_side_identities(proposed_state)

    latest_album, latest_state = _load_expected_album(
        server.album_path,
        expected_sha256=expected_sha256,
        expected_revision=expected_revision,
        recognition_provider=server.recognition_provider,
    )
    _assert_expected_sides(latest_state, expected_sides)
    if latest_album.to_dict() != load_album_project(server.album_path).to_dict():
        raise _AlbumConflictError(
            "The album project changed during mutation validation. Reload."
        )
    repeated_proposed_state = build_album_workbench_state(
        album,
        server.album_path,
        recognition_readiness=server.recognition_provider.readiness(),
    )
    _assert_no_stale_sources(repeated_proposed_state)
    if _state_side_identities(repeated_proposed_state) != proposed_identities:
        raise _AlbumConflictError(
            "An album side changed during mutation validation. Reload."
        )
    if _album_digest_or_conflict(server.album_path) != expected_sha256:
        raise _AlbumConflictError(
            "The album project changed during mutation validation. Reload."
        )
    _assert_fixed_album_destination(server.album_path)
    try:
        save_album_project(
            album,
            server.album_path,
            overwrite=True,
            expected_existing_sha256=expected_sha256,
        )
    except (OSError, ProjectValidationError) as exc:
        raise _AlbumConflictError(
            "The album project changed before its edit could be saved. Reload."
        ) from exc
    try:
        updated_album, updated_sha256 = load_album_project_with_sha256(
            server.album_path
        )
        updated_state = _workbench_state(
            updated_album,
            server.album_path,
            updated_sha256,
            server.recognition_provider,
        )
    except ProjectValidationError as exc:
        raise _AlbumConflictError(
            "An album reference changed while the saved state was verified. Reload."
        ) from exc
    if updated_album.revision != expected_revision + 1:
        raise _AlbumConflictError(
            "The album revision did not advance exactly once. Reload."
        )
    return updated_state


class AlbumReviewServer(ThreadingHTTPServer):
    """A local-only HTTP server bound to one predetermined album project."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        album_path: Path,
        *,
        recognition_provider: RecognitionProvider | None = None,
        musicbrainz_client: MusicBrainzClient | None = None,
        cover_art_client: CoverArtArchiveClient | None = None,
    ):
        host, port = address
        loopbacks = _loopback_addresses(host)
        if not loopbacks:
            raise ValueError("Album review server host must resolve only to loopback.")
        family, resolved_host = next(
            (item for item in loopbacks if item[0] == socket.AF_INET),
            loopbacks[0],
        )
        self.album_path = canonical_album_path(album_path)
        self.session_auth = LoopbackSessionAuth()
        load_album_project(self.album_path)
        self.operation_lock = threading.Lock()
        self.recognition_lock = threading.Lock()
        self.recognition_provider = (
            AcoustIDRecognitionProvider()
            if recognition_provider is None
            else recognition_provider
        )
        self.musicbrainz_client = (
            MusicBrainzClient() if musicbrainz_client is None else musicbrainz_client
        )
        self.cover_art_client = (
            CoverArtArchiveClient(
                self.album_path.parent,
                artwork_folder="artwork/review",
            )
            if cover_art_client is None
            else cover_art_client
        )
        self.metadata_network_lock = threading.Lock()
        self.release_review_lock = threading.Lock()
        self.release_reviews: dict[str, dict[str, Any]] = {}
        self.artwork_previews: dict[str, _AlbumArtworkPreview] = {}
        self._side_review_lock = threading.Lock()
        self._side_review_children: dict[str, _SideReviewChild] = {}
        self._side_review_closing = False
        self.address_family = family
        super().__init__((resolved_host, port), AlbumReviewHandler)

    @staticmethod
    def _wait_for_side_review(child: _SideReviewChild) -> None:
        """Prove the new child is serving before its URL is returned."""

        address = child.server.server_address
        if not isinstance(address, tuple) or len(address) != 2:
            raise RuntimeError("Side review server returned an invalid endpoint.")
        host, port = address
        if not isinstance(host, str) or type(port) is not int:
            raise RuntimeError("Side review server returned an invalid endpoint.")
        connection = http.client.HTTPConnection(host, port, timeout=5.0)
        try:
            connection.request(
                "GET",
                "/",
                headers={
                    "Host": f"{child.server.session_auth.public_host}:{port}",
                },
            )
            response = connection.getresponse()
            response.read()
            if response.status != HTTPStatus.OK:
                raise RuntimeError("Side review server failed its startup check.")
        finally:
            connection.close()

    def retire_stale_side_review(
        self,
        side_label: str,
        current_identity: Mapping[str, Any],
    ) -> None:
        """Close a child that no longer represents the side's exact identity."""

        stale: _SideReviewChild | None = None
        with self._side_review_lock:
            existing = self._side_review_children.get(side_label)
            if existing is not None and existing.current_identity != current_identity:
                stale = self._side_review_children.pop(side_label)
        if stale is not None:
            stale.close()

    def open_side_review(
        self,
        side_label: str,
        project_path: Path,
        current_identity: dict[str, int | str],
    ) -> tuple[_SideReviewChild, bool]:
        """Reuse or start the child bound to one exact side-project identity."""

        stale: _SideReviewChild | None = None
        with self._side_review_lock:
            if self._side_review_closing:
                raise GrooveSerpentError("The Album Workbench is shutting down.")
            existing = self._side_review_children.get(side_label)
            if existing is not None:
                if (
                    existing.project_path == project_path
                    and existing.current_identity == current_identity
                    and existing.thread.is_alive()
                ):
                    existing.server.session_auth.rearm_bootstrap_if_consumed()
                    existing.url = _review_server_url(existing.server)
                    return existing, True
                stale = self._side_review_children.pop(side_label)
        if stale is not None:
            stale.close()

        child_server = ReviewServer(("127.0.0.1", 0), project_path)
        thread: threading.Thread | None = None
        try:
            url = _review_server_url(child_server)
            thread = threading.Thread(
                target=child_server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name=f"groove-serpent-side-{side_label}-review",
                daemon=True,
            )
            child = _SideReviewChild(
                side_label=side_label,
                project_path=project_path,
                current_identity=dict(current_identity),
                server=child_server,
                thread=thread,
                url=url,
            )
            thread.start()
            self._wait_for_side_review(child)
        except BaseException:
            if thread is not None and thread.is_alive():
                child_server.shutdown()
                thread.join(timeout=5.0)
            child_server.server_close()
            raise

        close_new_child = False
        winner: _SideReviewChild | None = None
        displaced: _SideReviewChild | None = None
        with self._side_review_lock:
            if self._side_review_closing:
                close_new_child = True
            else:
                existing = self._side_review_children.get(side_label)
                if (
                    existing is not None
                    and existing.project_path == project_path
                    and existing.current_identity == current_identity
                    and existing.thread.is_alive()
                ):
                    winner = existing
                else:
                    if existing is not None:
                        displaced = self._side_review_children.pop(side_label)
                    self._side_review_children[side_label] = child
        if close_new_child:
            child.close()
            raise GrooveSerpentError("The Album Workbench is shutting down.")
        if winner is not None:
            child.close()
            return winner, True
        if displaced is not None:
            displaced.close()
        return child, False

    def retire_side_review(self, side_label: str) -> None:
        child: _SideReviewChild | None
        with self._side_review_lock:
            child = self._side_review_children.pop(side_label, None)
        if child is not None:
            child.close()

    def _close_side_reviews(self) -> None:
        with self._side_review_lock:
            self._side_review_closing = True
            children = list(self._side_review_children.values())
            self._side_review_children.clear()
        failure: BaseException | None = None
        for child in children:
            try:
                child.close()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure

    def server_close(self) -> None:
        """Close every child listener and snapshot before the album listener."""

        failure: BaseException | None = None
        try:
            self._close_side_reviews()
        except BaseException as exc:
            failure = exc
        finally:
            super().server_close()
        if failure is not None:
            raise failure

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(
            sys.exception(),
            (BrokenPipeError, ConnectionAbortedError, ConnectionResetError),
        ):
            return
        super().handle_error(request, client_address)


class AlbumReviewHandler(BaseHTTPRequestHandler):
    server: AlbumReviewServer
    protocol_version = "HTTP/1.1"

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        self._session_authentication: SessionAuthentication | None = None
        if not request_target_is_exact(self.requestline, self.path):
            self.close_connection = True
            self._error(HTTPStatus.BAD_REQUEST, "Invalid request target.")
            return False
        authority = self._request_authority()
        if authority is None:
            self.close_connection = True
            self._error(HTTPStatus.BAD_REQUEST, "Invalid Host header.")
            return False
        self._validated_authority = authority
        if not self._request_has_session_access():
            self.close_connection = True
            self._unauthorized()
            return False
        return True

    def _request_has_session_access(self) -> bool:
        if self.command == "GET" and self.path in _STATIC_ROUTES:
            return True
        if self.command == "GET" and self.server.session_auth.is_bootstrap_target(self.path):
            return True
        self._session_authentication = self.server.session_auth.authentication_method(
            self.headers
        )
        return self._session_authentication is not None

    def end_headers(self) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; media-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if self.close_connection:
            self.send_header("Connection", "close")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return

    def _request_authority(self) -> tuple[str, int] | None:
        values = self.headers.get_all("Host", [])
        if len(values) != 1:
            return None
        value = values[0]
        if value != value.strip() or any(character.isspace() for character in value):
            return None
        try:
            parsed = urlsplit(f"//{value}")
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            return None
        if (
            host is None
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or port != self.server.server_port
            or _normalized_host(host) != self.server.session_auth.public_host
        ):
            return None
        return (_normalized_host(host), port)

    def _origin_is_same_origin(self, value: str) -> bool:
        if value != value.strip() or any(character.isspace() for character in value):
            return False
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            return False
        if (
            parsed.scheme.casefold() != "http"
            or not parsed.netloc
            or host is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return False
        effective_port = 80 if port is None else port
        return (_normalized_host(host), effective_port) == self._validated_authority

    def _validate_post_headers(self) -> bool:
        if self.headers.get_all("Transfer-Encoding", []):
            self.close_connection = True
            self._error(HTTPStatus.BAD_REQUEST, "Transfer-Encoding is not supported.")
            return False
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) != 1 or self.headers.get_content_type() != "application/json":
            self._discard_declared_request_body()
            self.close_connection = True
            self._error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Content-Type must be application/json.",
            )
            return False
        origins = self.headers.get_all("Origin", [])
        origin_required = self._session_authentication == "cookie"
        if (
            len(origins) > 1
            or (origin_required and len(origins) != 1)
            or (origins and not self._origin_is_same_origin(origins[0]))
        ):
            self._discard_declared_request_body()
            self.close_connection = True
            self._error(HTTPStatus.FORBIDDEN, "Origin does not match this server.")
            return False
        return True

    def _discard_declared_request_body(self) -> None:
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            return
        try:
            remaining = int(lengths[0])
        except ValueError:
            return
        if remaining < 0 or remaining > _MAX_REQUEST_BODY:
            return
        while remaining:
            chunk = self.rfile.read(min(16 * 1024, remaining))
            if not chunk:
                return
            remaining -= len(chunk)

    def _read_json(self) -> dict[str, Any]:
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self.close_connection = True
            raise ProjectValidationError("Invalid Content-Length header.")
        try:
            length = int(lengths[0])
        except ValueError as exc:
            self.close_connection = True
            raise ProjectValidationError("Invalid Content-Length header.") from exc
        if length <= 0 or length > _MAX_REQUEST_BODY:
            self.close_connection = True
            raise ProjectValidationError("Request body is missing or too large.")
        raw_body = self.rfile.read(length)
        if len(raw_body) != length:
            self.close_connection = True
            raise ProjectValidationError("Request body is incomplete.")
        try:
            payload = json.loads(
                raw_body.decode("utf-8"),
                object_pairs_hook=_strict_object_pairs,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            self.close_connection = True
            raise ProjectValidationError("Request body is not valid JSON.") from exc
        if type(payload) is not dict:
            self.close_connection = True
            raise ProjectValidationError("Request body must be a JSON object.")
        return cast(dict[str, Any], payload)

    def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json({"ok": False, "error": message}, status=status)

    def _unauthorized(self) -> None:
        body = b'{"ok":false,"error":"Session authentication required."}'
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.wfile.write(body)

    def _bootstrap_session(self) -> None:
        consumed = self.server.session_auth.consume_bootstrap(self.path)
        if not consumed and not self.server.session_auth.authenticated(self.headers):
            self._unauthorized()
            return
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        if consumed:
            self.send_header("Set-Cookie", self.server.session_auth.set_cookie_header)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _static(self, name: str, content_type: str) -> None:
        body = files("groove_serpent").joinpath("web", name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.server.session_auth.is_bootstrap_target(self.path):
            self._bootstrap_session()
            return
        try:
            parsed = urlsplit(self.path)
            route = parsed.path
            static = _STATIC_ROUTES.get(route)
            if static is not None:
                self._static(*static)
            elif route == "/api/ping":
                self._json({"ok": True})
            elif route == "/api/album/state":
                with self.server.operation_lock:
                    album, digest = load_album_project_with_sha256(
                        self.server.album_path
                    )
                    state = _workbench_state(
                        album,
                        self.server.album_path,
                        digest,
                        self.server.recognition_provider,
                    )
                    if _album_digest_or_conflict(self.server.album_path) != digest:
                        raise _AlbumConflictError(
                            "The album project changed while its state was loaded. Reload."
                        )
                self._json(state)
            elif route.startswith("/api/album/identification/artwork-preview/"):
                if parsed.query or parsed.fragment:
                    raise ProjectValidationError(
                        "Artwork preview does not accept query parameters."
                    )
                digest = _strict_digest(
                    route.removeprefix(
                        "/api/album/identification/artwork-preview/"
                    ),
                    "Artwork preview SHA-256",
                )
                self._serve_album_artwork_preview(digest)
            else:
                self._error(HTTPStatus.NOT_FOUND, "Not found")
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except _AlbumConflictError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except ProjectValidationError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unexpected server error.")

    def _serve_album_artwork_preview(self, digest: str) -> None:
        with self.server.release_review_lock:
            preview = self.server.artwork_previews.get(digest)
        if preview is None:
            self._error(HTTPStatus.NOT_FOUND, "No current reviewed artwork has this identity.")
            return
        with self.server.operation_lock:
            album, album_sha256 = load_album_project_with_sha256(
                self.server.album_path
            )
            if (
                album_sha256 != preview.album_sha256
                or album.revision != preview.album_revision
            ):
                raise _AlbumConflictError(
                    "The album changed after artwork review. Reload and review again."
                )
            state = _workbench_state(
                album,
                self.server.album_path,
                album_sha256,
                self.server.recognition_provider,
            )
            _current_identification_candidate(
                self.server.album_path,
                state,
                filename=preview.proposal_filename,
                file_sha256=preview.proposal_file_sha256,
                proposal_sha256=preview.proposal_sha256,
                release_mbid=preview.release_mbid,
            )
            _path, body = _safe_review_artwork_path(
                self.server.album_path,
                relative_path=preview.relative_path,
                expected_sha256=preview.sha256,
                expected_size=preview.size_bytes,
            )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", preview.mime_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment:
                self._discard_declared_request_body()
                self.close_connection = True
                raise ProjectValidationError(
                    "Album mutation endpoints do not accept query parameters."
                )
            if not self._validate_post_headers():
                return
            if parsed.path == "/api/album/repin":
                self._repin()
            elif parsed.path == "/api/album/open-side":
                self._open_side()
            elif parsed.path == "/api/album/add-side":
                self._add_side()
            elif parsed.path == "/api/album/remove-side":
                self._remove_side()
            elif parsed.path == "/api/album/reorder-sides":
                self._reorder_sides()
            elif parsed.path == "/api/album/update-details":
                self._update_details()
            elif parsed.path == "/api/album/identification/scan":
                self._scan_album_identification()
            elif parsed.path == "/api/album/identification/open-proposal":
                self._open_album_identification_proposal()
            elif parsed.path == "/api/album/identification/release-details":
                self._review_album_release_details()
            elif parsed.path == "/api/album/identification/download-artwork":
                self._download_album_candidate_artwork()
            elif parsed.path == "/api/album/publication/create-plan":
                self._create_publication_plan()
            elif parsed.path == "/api/album/publication/preflight":
                self._preflight_publication_plan()
            elif parsed.path == "/api/album/publication/execute":
                self._execute_publication_plan()
            elif parsed.path == "/api/album/publication/verify":
                self._verify_publication_receipt()
            elif parsed.path == "/api/album/publication/replay":
                self._replay_publication_receipt()
            elif parsed.path == "/api/album/publication/recover":
                self._recover_publication_orphan()
            else:
                self._discard_declared_request_body()
                self.close_connection = True
                self._error(HTTPStatus.NOT_FOUND, "Not found")
                return
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except _AlbumConflictError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except ProjectValidationError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unexpected server error.")

    def _repin(self) -> None:
        payload = self._read_json()
        fields = frozenset(
            {
                "expected_album_sha256",
                "expected_album_revision",
                "side_label",
                "expected_current_identity",
                "reviewed",
            }
        )
        _strict_object(payload, fields=fields, label="Album repin request")
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        side_label = _strict_side_label(payload["side_label"])
        expected_identity = _expected_identity(payload["expected_current_identity"])
        if payload["reviewed"] is not True:
            raise ProjectValidationError(
                "Repinning requires reviewed to be the JSON boolean true."
            )

        with self.server.operation_lock:
            _assert_fixed_album_destination(self.server.album_path)
            current_album_sha256 = _album_digest_or_conflict(self.server.album_path)
            if current_album_sha256 != expected_album_sha256:
                raise _AlbumConflictError(
                    "The album project changed after this review page loaded. Reload."
                )
            try:
                album, album_sha256 = load_album_project_with_sha256(
                    self.server.album_path
                )
            except ProjectValidationError as exc:
                raise _AlbumConflictError(
                    "The album project changed after this review page loaded. Reload."
                ) from exc
            if album_sha256 != expected_album_sha256:
                raise _AlbumConflictError(
                    "The album project changed after this review page loaded. Reload."
                )
            if album.revision != expected_album_revision:
                raise _AlbumConflictError(
                    "The album revision changed after this review page loaded. Reload."
                )
            try:
                state = _workbench_state(
                    album,
                    self.server.album_path,
                    album_sha256,
                    self.server.recognition_provider,
                )
            except ProjectValidationError as exc:
                raise _AlbumConflictError(
                    "An album side changed while its review state was loaded. Reload."
                ) from exc
            current_identity = _side_current_identity(state, side_label)
            for key in _EXPECTED_IDENTITY_KEYS:
                if current_identity[key] != expected_identity[key]:
                    raise _AlbumConflictError(
                        f"Side {side_label} changed after it was reviewed. Reload."
                    )

            try:
                repinned = repin_album_sides(
                    album,
                    self.server.album_path,
                    [side_label],
                )
            except (OSError, ProjectValidationError) as exc:
                raise _AlbumConflictError(
                    f"Side {side_label} changed while it was being repinned. Reload."
                ) from exc
            if repinned != [side_label]:
                raise RuntimeError("Album repin selected an unexpected side.")
            selected = next(side for side in album.sides if side.label == side_label)
            if selected.pin is None:
                raise RuntimeError("Album repin did not create a side pin.")
            new_pin_identity: dict[str, int | str] = {
                "project_revision": selected.pin.project_revision,
                "project_sha256": selected.pin.project_sha256,
                "editable_state_sha256": selected.pin.editable_state_sha256,
                "source_sha256": selected.pin.source_sha256,
                "project_speed_state_sha256": (
                    selected.pin.project_speed_state_sha256
                ),
            }
            if new_pin_identity != expected_identity:
                raise _AlbumConflictError(
                    f"Side {side_label} changed while it was being repinned. Reload."
                )
            if _album_digest_or_conflict(self.server.album_path) != album_sha256:
                raise _AlbumConflictError(
                    "The album project changed while the side was being repinned. Reload."
                )
            _assert_fixed_album_destination(self.server.album_path)
            save_album_project(
                album,
                self.server.album_path,
                overwrite=True,
                expected_existing_sha256=album_sha256,
            )
            updated_album, updated_sha256 = load_album_project_with_sha256(
                self.server.album_path
            )
            if updated_album.revision != expected_album_revision + 1:
                raise _AlbumConflictError(
                    "The album revision did not advance exactly once. Reload."
                )
            updated_state = _workbench_state(
                updated_album,
                self.server.album_path,
                updated_sha256,
                self.server.recognition_provider,
            )
            if _album_digest_or_conflict(self.server.album_path) != updated_sha256:
                raise _AlbumConflictError(
                    "The album project changed while its new state was loaded. Reload."
                )
        self._json(updated_state)

    def _add_side(self) -> None:
        payload = self._read_json()
        _strict_object(
            payload,
            fields=frozenset(
                {
                    "expected_album_sha256",
                    "expected_album_revision",
                    "expected_sides",
                    "side_label",
                    "project_reference",
                }
            ),
            label="Add-side request",
        )
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        expected_sides = _expected_sides(payload["expected_sides"])
        side_label = _strict_side_label(payload["side_label"])
        project_reference = _strict_relative_reference(
            payload["project_reference"],
            "Album side project reference",
        )

        with self.server.operation_lock:
            album, state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(state, expected_sides)
            _assert_no_stale_sources(state)
            side = _new_unpinned_side(
                self.server.album_path,
                label=side_label,
                project_reference=project_reference,
                order=len(album.sides) + 1,
            )
            album.sides.append(side)
            album.validate()
            updated_state = _save_album_mutation(
                self.server,
                album,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                expected_sides=expected_sides,
            )
        self._json(updated_state)

    def _remove_side(self) -> None:
        payload = self._read_json()
        _strict_object(
            payload,
            fields=frozenset(
                {
                    "expected_album_sha256",
                    "expected_album_revision",
                    "expected_sides",
                    "side_label",
                    "confirmation",
                }
            ),
            label="Remove-side request",
        )
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        expected_sides = _expected_sides(payload["expected_sides"])
        side_label = _strict_side_label(payload["side_label"])
        confirmation = payload["confirmation"]
        if confirmation != f"REMOVE {side_label}":
            raise ProjectValidationError(
                f"Removing Side {side_label} requires typing REMOVE {side_label}."
            )

        with self.server.operation_lock:
            album, state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(state, expected_sides)
            _assert_no_stale_sources(state)
            selected = [side for side in album.sides if side.label == side_label]
            if len(selected) != 1:
                raise ProjectValidationError(
                    f"Unknown exact album side label: {side_label!r}."
                )
            if len(album.sides) == 1:
                raise ProjectValidationError(
                    "The album must retain at least one side."
                )
            album.sides = [side for side in album.sides if side.label != side_label]
            for order, side in enumerate(album.sides, start=1):
                side.order = order
                side.pin = None
            album.validate()
            updated_state = _save_album_mutation(
                self.server,
                album,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                expected_sides=expected_sides,
            )
        self.server.retire_side_review(side_label)
        self._json(updated_state)

    def _reorder_sides(self) -> None:
        payload = self._read_json()
        _strict_object(
            payload,
            fields=frozenset(
                {
                    "expected_album_sha256",
                    "expected_album_revision",
                    "expected_sides",
                    "ordered_side_labels",
                    "approval_acknowledged",
                }
            ),
            label="Reorder-sides request",
        )
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        expected_sides = _expected_sides(payload["expected_sides"])
        raw_labels = payload["ordered_side_labels"]
        if type(raw_labels) is not list or not 1 <= len(raw_labels) <= _MAX_ALBUM_SIDES:
            raise ProjectValidationError(
                "Ordered side labels must be a non-empty bounded JSON array."
            )
        ordered_labels = [_strict_side_label(value) for value in raw_labels]
        folded_labels = [portable_name_key(value) for value in ordered_labels]
        if len(folded_labels) != len(set(folded_labels)):
            raise ProjectValidationError(
                "Ordered side labels cannot contain portable-equivalent duplicates."
            )
        if payload["approval_acknowledged"] is not True:
            raise ProjectValidationError(
                "Reordering requires acknowledgement that every side pin is cleared."
            )

        with self.server.operation_lock:
            album, state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(state, expected_sides)
            _assert_no_stale_sources(state)
            by_label = {portable_name_key(side.label): side for side in album.sides}
            if set(folded_labels) != set(by_label) or len(folded_labels) != len(
                album.sides
            ):
                raise ProjectValidationError(
                    "The ordered labels must name every current side exactly once."
                )
            current_labels = [side.label for side in album.sides]
            if ordered_labels == current_labels:
                raise ProjectValidationError("Choose a different side order.")
            album.sides = [by_label[label] for label in folded_labels]
            for order, side in enumerate(album.sides, start=1):
                side.order = order
                side.pin = None
            album.validate()
            updated_state = _save_album_mutation(
                self.server,
                album,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                expected_sides=expected_sides,
            )
        self._json(updated_state)

    def _update_details(self) -> None:
        payload = self._read_json()
        _strict_object(
            payload,
            fields=frozenset(
                {
                    "expected_album_sha256",
                    "expected_album_revision",
                    "expected_sides",
                    "metadata",
                    "artwork_path",
                    "expected_artwork_sha256",
                }
            ),
            label="Update-album-details request",
        )
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        expected_sides = _expected_sides(payload["expected_sides"])
        metadata = _strict_metadata(payload["metadata"])
        raw_artwork_path = payload["artwork_path"]
        artwork_path = (
            None
            if raw_artwork_path is None
            else _strict_relative_reference(raw_artwork_path, "Album artwork path")
        )
        raw_expected_artwork_sha256 = payload["expected_artwork_sha256"]
        expected_artwork_sha256 = (
            None
            if raw_expected_artwork_sha256 is None
            else _strict_digest(
                raw_expected_artwork_sha256,
                "Expected artwork SHA-256",
            )
        )
        if artwork_path is None and expected_artwork_sha256 is not None:
            raise ProjectValidationError(
                "Expected artwork SHA-256 requires an artwork path."
            )
        if (
            artwork_path is not None
            and artwork_path.startswith("artwork/review/")
            and expected_artwork_sha256 is None
        ):
            raise ProjectValidationError(
                "Downloaded review artwork requires its exact reviewed SHA-256."
            )

        with self.server.operation_lock:
            album, state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(state, expected_sides)
            _assert_no_stale_sources(state)
            artwork = (
                None
                if artwork_path is None
                else artwork_for_album_path(
                    self.server.album_path,
                    Path(artwork_path),
                )
            )
            if (
                artwork is not None
                and expected_artwork_sha256 is not None
                and artwork.sha256 != expected_artwork_sha256
            ):
                raise _AlbumConflictError(
                    "Artwork bytes changed after review. Reload and review the exact file."
                )
            if album.metadata == metadata and album.artwork == artwork:
                raise ProjectValidationError("Album details did not change.")
            album.metadata = metadata
            album.artwork = artwork
            album.validate()
            updated_state = _save_album_mutation(
                self.server,
                album,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                expected_sides=expected_sides,
            )
        self._json(updated_state)

    def _scan_album_identification(self) -> None:
        payload = self._read_json()
        _strict_object(
            payload,
            fields=frozenset(
                {
                    "expected_album_sha256",
                    "expected_album_revision",
                    "expected_sides",
                    "action",
                    "network_reviewed",
                }
            ),
            label="Scan-album-identification request",
        )
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        expected_sides = _expected_sides(payload["expected_sides"])
        if payload["action"] != "scan-current-track-fingerprints":
            raise ProjectValidationError(
                "Album identification requires the exact deliberate scan action."
            )
        if payload["network_reviewed"] is not True:
            raise ProjectValidationError(
                "Album identification requires network_reviewed to be true."
            )
        if not self.server.recognition_lock.acquire(blocking=False):
            raise ProjectValidationError(
                "Another album identification scan is already running."
            )

        snapshots: list[VerifiedAudioSnapshot] = []
        try:
            readiness = self.server.recognition_provider.readiness()
            if not readiness.ready:
                raise ProjectValidationError(readiness.message)
            with self.server.operation_lock:
                _album, state = _load_expected_album(
                    self.server.album_path,
                    expected_sha256=expected_album_sha256,
                    expected_revision=expected_album_revision,
                    recognition_provider=self.server.recognition_provider,
                )
                _assert_expected_sides(state, expected_sides)
                identification = _identification_state(state)
                catalog = identification.get("catalog")
                if type(catalog) is not dict or catalog.get("scan_complete") is not True:
                    raise ProjectValidationError(
                        "The identification proposal catalog is incomplete; scan refused."
                    )
                try:
                    context = capture_album_identification_context(
                        self.server.album_path
                    )
                except ProjectValidationError as exc:
                    raise ProjectValidationError(
                        "The album is not ready for exact identification: "
                        f"{str(exc)[:1_024]}"
                    ) from exc
                if (
                    context.album_sha256 != expected_album_sha256
                    or context.album_revision != expected_album_revision
                ):
                    raise _AlbumConflictError(
                        "The album changed before fingerprint capture. Reload."
                    )
                try:
                    inputs, snapshots = _snapshot_album_identification_inputs(
                        self.server.album_path,
                        context,
                    )
                    repeated_context = capture_album_identification_context(
                        self.server.album_path
                    )
                except ProjectValidationError as exc:
                    raise _AlbumConflictError(
                        "An album source changed during fingerprint snapshot capture. Reload."
                    ) from exc
                if repeated_context != context:
                    raise _AlbumConflictError(
                        "The album context changed during fingerprint snapshot capture. Reload."
                    )

            evidence: list[TrackRecognitionEvidence] = []
            unmatched_track_count = 0
            total_match_count = 0
            for side_label, project, snapshot in inputs:
                side = context.side(side_label)
                for track in side.tracks:
                    try:
                        snapshot.assert_snapshot_unchanged(force=True)
                        snapshot.assert_live_unchanged(force=True)
                    except ProjectValidationError as exc:
                        raise _AlbumConflictError(
                            "An album source changed before fingerprinting completed. Reload."
                        ) from exc
                    try:
                        matches = self.server.recognition_provider.identify_track(
                            snapshot,
                            track.start_sample,
                            track.end_sample,
                            project.source.sample_rate,
                            source_speed_factor=side.requested_speed_factor,
                        )
                    except RecognitionError as exc:
                        raise ProjectValidationError(
                            f"Album identification failed: {str(exc)[:1_024]}"
                        ) from exc
                    if type(matches) is not list or len(matches) > MAX_MATCHES_PER_TRACK:
                        raise ProjectValidationError(
                            "The recognition provider returned an invalid match count."
                        )
                    try:
                        snapshot.assert_snapshot_unchanged(force=True)
                        snapshot.assert_live_unchanged(force=True)
                    except ProjectValidationError as exc:
                        raise _AlbumConflictError(
                            "An album source changed while fingerprinting was running. Reload."
                        ) from exc
                    if matches:
                        if total_match_count + len(matches) > MAX_TOTAL_MATCHES:
                            raise ProjectValidationError(
                                "The album recognition result exceeded its match budget."
                            )
                        evidence.append(
                            context.bind_track(side_label, track.number, matches)
                        )
                        total_match_count += len(matches)
                    else:
                        unmatched_track_count += 1

            with self.server.operation_lock:
                try:
                    for snapshot in snapshots:
                        snapshot.assert_snapshot_unchanged(force=True)
                        snapshot.assert_live_unchanged(force=True)
                    current_context = capture_album_identification_context(
                        self.server.album_path
                    )
                except ProjectValidationError as exc:
                    raise _AlbumConflictError(
                        "The album context changed while fingerprinting was running. Reload."
                    ) from exc
                if current_context != context:
                    raise _AlbumConflictError(
                        "The album context changed while fingerprinting was running. Reload."
                    )
                _current_album, latest_state = _load_expected_album(
                    self.server.album_path,
                    expected_sha256=expected_album_sha256,
                    expected_revision=expected_album_revision,
                    recognition_provider=self.server.recognition_provider,
                )
                _assert_expected_sides(latest_state, expected_sides)

                proposal: dict[str, Any] | None = None
                catalog_entry: dict[str, Any] | None = None
                completion = "abstained-no-matches"
                status = HTTPStatus.OK
                if evidence:
                    proposal = propose_album_release_identification(
                        self.server.album_path,
                        evidence,
                    )
                    proposal_sha256 = cast(str, proposal["proposal_sha256"])
                    destination = album_identification_proposal_path(
                        self.server.album_path,
                        proposal,
                    )
                    reused = False
                    try:
                        saved_path = save_album_identification_proposal(
                            self.server.album_path,
                            proposal,
                        )
                    except ProjectValidationError as save_error:
                        try:
                            loaded = load_current_album_identification_proposal(
                                self.server.album_path,
                                destination,
                            )
                        except ProjectValidationError:
                            raise save_error
                        if loaded.proposal != proposal:
                            raise _AlbumConflictError(
                                "A different identification proposal occupies this identity."
                            ) from save_error
                        saved_path = loaded.path
                        reused = True
                    _verified_album, latest_state = _load_expected_album(
                        self.server.album_path,
                        expected_sha256=expected_album_sha256,
                        expected_revision=expected_album_revision,
                        recognition_provider=self.server.recognition_provider,
                    )
                    _assert_expected_sides(latest_state, expected_sides)
                    catalog_entry = _identification_catalog_entry(
                        latest_state,
                        filename=saved_path.name,
                        proposal_sha256=proposal_sha256,
                    )
                    completion = "proposal-reused" if reused else "proposal-created"
                    status = HTTPStatus.OK if reused else HTTPStatus.CREATED

                for snapshot in snapshots:
                    try:
                        snapshot.assert_snapshot_unchanged(force=True)
                        snapshot.assert_live_unchanged(force=True)
                    except ProjectValidationError as exc:
                        raise _AlbumConflictError(
                            "An album source changed before scan completion. Reload."
                        ) from exc
        finally:
            for snapshot in reversed(snapshots):
                snapshot.close()
            self.server.recognition_lock.release()

        self._json(
            {
                "ok": True,
                "completion": completion,
                "network_request_performed": True,
                "provider": readiness.to_dict(),
                "scan": {
                    "album_context_sha256": context.sha256,
                    "album_track_count": sum(
                        len(side.tracks) for side in context.sides
                    ),
                    "matched_track_count": len(evidence),
                    "unmatched_track_count": unmatched_track_count,
                    "total_match_count": total_match_count,
                },
                "proposal": proposal,
                "catalog_entry": catalog_entry,
                "state": latest_state,
            },
            status=status,
        )

    def _open_album_identification_proposal(self) -> None:
        payload = self._read_json()
        _strict_object(
            payload,
            fields=frozenset(
                {
                    "expected_album_sha256",
                    "expected_album_revision",
                    "expected_sides",
                    "action",
                    "filename",
                    "file_sha256",
                    "proposal_sha256",
                }
            ),
            label="Open-album-identification-proposal request",
        )
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        expected_sides = _expected_sides(payload["expected_sides"])
        filename = _strict_identification_proposal_filename(payload["filename"])
        file_sha256 = _strict_digest(
            payload["file_sha256"], "Identification proposal file SHA-256"
        )
        proposal_sha256 = _strict_digest(
            payload["proposal_sha256"], "Identification proposal SHA-256"
        )
        if payload["action"] != "open-current-identification-proposal":
            raise ProjectValidationError(
                "Opening an identification proposal requires the exact read-only action."
            )

        with self.server.operation_lock:
            _album, state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(state, expected_sides)
            catalog_entry = _identification_catalog_entry(
                state,
                filename=filename,
                proposal_sha256=proposal_sha256,
                file_sha256=file_sha256,
            )
            proposal_path = self.server.album_path.parent / filename
            loaded = load_current_album_identification_proposal(
                self.server.album_path,
                proposal_path,
                expected_file_sha256=file_sha256,
            )
            if loaded.proposal.get("proposal_sha256") != proposal_sha256:
                raise _AlbumConflictError(
                    "The identification proposal identity changed. Reload."
                )
            _latest_album, latest_state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(latest_state, expected_sides)
            latest_entry = _identification_catalog_entry(
                latest_state,
                filename=filename,
                proposal_sha256=proposal_sha256,
                file_sha256=file_sha256,
            )
            repeated = load_current_album_identification_proposal(
                self.server.album_path,
                proposal_path,
                expected_file_sha256=file_sha256,
            )
            if repeated.proposal != loaded.proposal or latest_entry != catalog_entry:
                raise _AlbumConflictError(
                    "The identification proposal changed while it was opened. Reload."
                )
        self._json(
            {
                "ok": True,
                "read_only": True,
                "proposal": loaded.proposal,
                "catalog_entry": latest_entry,
                "state": latest_state,
            }
        )

    def _review_album_release_details(self) -> None:
        payload = self._read_json()
        _strict_object(
            payload,
            fields=frozenset(
                {
                    "expected_album_sha256",
                    "expected_album_revision",
                    "expected_sides",
                    "action",
                    "network_reviewed",
                    "proposal_filename",
                    "proposal_file_sha256",
                    "proposal_sha256",
                    "release_mbid",
                }
            ),
            label="Review-album-release-details request",
        )
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        expected_sides = _expected_sides(payload["expected_sides"])
        filename = _strict_identification_proposal_filename(
            payload["proposal_filename"]
        )
        file_sha256 = _strict_digest(
            payload["proposal_file_sha256"], "Identification proposal file SHA-256"
        )
        proposal_sha256 = _strict_digest(
            payload["proposal_sha256"], "Identification proposal SHA-256"
        )
        release_mbid = _strict_release_mbid(payload["release_mbid"])
        if payload["action"] != "fetch-current-candidate-release-details":
            raise ProjectValidationError(
                "Release detail review requires the exact deliberate fetch action."
            )
        if payload["network_reviewed"] is not True:
            raise ProjectValidationError(
                "Release detail review requires network_reviewed to be true."
            )
        if not self.server.metadata_network_lock.acquire(blocking=False):
            raise ProjectValidationError("Another metadata network action is already running.")
        try:
            with self.server.operation_lock:
                _album, state = _load_expected_album(
                    self.server.album_path,
                    expected_sha256=expected_album_sha256,
                    expected_revision=expected_album_revision,
                    recognition_provider=self.server.recognition_provider,
                )
                _assert_expected_sides(state, expected_sides)
                _assert_no_stale_sources(state)
                _proposal, _candidate, _entry, binding = (
                    _current_identification_candidate(
                        self.server.album_path,
                        state,
                        filename=filename,
                        file_sha256=file_sha256,
                        proposal_sha256=proposal_sha256,
                        release_mbid=release_mbid,
                    )
                )
            try:
                raw_release = self.server.musicbrainz_client.get_release(release_mbid)
            except MetadataLookupError as exc:
                raise ProjectValidationError(
                    f"MusicBrainz release review failed: {str(exc)[:1_024]}"
                ) from exc
            release = _normalize_release_review(raw_release, release_mbid)
            release_sha256 = canonical_json_sha256(release)

            with self.server.operation_lock:
                _latest_album, latest_state = _load_expected_album(
                    self.server.album_path,
                    expected_sha256=expected_album_sha256,
                    expected_revision=expected_album_revision,
                    recognition_provider=self.server.recognition_provider,
                )
                _assert_expected_sides(latest_state, expected_sides)
                _assert_no_stale_sources(latest_state)
                _p, _c, _e, repeated_binding = _current_identification_candidate(
                    self.server.album_path,
                    latest_state,
                    filename=filename,
                    file_sha256=file_sha256,
                    proposal_sha256=proposal_sha256,
                    release_mbid=release_mbid,
                )
                if repeated_binding != binding:
                    raise _AlbumConflictError(
                        "The album release candidate changed during detail lookup. Reload."
                    )
            review_body: dict[str, Any] = {
                "schema": _ALBUM_RELEASE_REVIEW_SCHEMA,
                "binding": binding,
                "release": release,
                "release_sha256": release_sha256,
                "authority": _release_review_authority(artwork_downloaded=False),
            }
            review = dict(review_body)
            review["review_sha256"] = canonical_json_sha256(review_body)
            with self.server.release_review_lock:
                self.server.release_reviews[cast(str, review["review_sha256"])] = (
                    copy.deepcopy(review)
                )
        finally:
            self.server.metadata_network_lock.release()
        self._json(
            {
                "ok": True,
                "network_request_performed": True,
                "review": review,
                "state": latest_state,
            }
        )

    def _download_album_candidate_artwork(self) -> None:
        payload = self._read_json()
        _strict_object(
            payload,
            fields=frozenset(
                {
                    "expected_album_sha256",
                    "expected_album_revision",
                    "expected_sides",
                    "action",
                    "network_reviewed",
                    "proposal_filename",
                    "proposal_file_sha256",
                    "proposal_sha256",
                    "release_mbid",
                    "expected_release_review_sha256",
                }
            ),
            label="Download-album-candidate-artwork request",
        )
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        expected_sides = _expected_sides(payload["expected_sides"])
        filename = _strict_identification_proposal_filename(
            payload["proposal_filename"]
        )
        file_sha256 = _strict_digest(
            payload["proposal_file_sha256"], "Identification proposal file SHA-256"
        )
        proposal_sha256 = _strict_digest(
            payload["proposal_sha256"], "Identification proposal SHA-256"
        )
        release_mbid = _strict_release_mbid(payload["release_mbid"])
        release_review_sha256 = _strict_digest(
            payload["expected_release_review_sha256"],
            "Expected release-review SHA-256",
        )
        if payload["action"] != "download-reviewed-candidate-front-artwork":
            raise ProjectValidationError(
                "Artwork review requires the exact deliberate download action."
            )
        if payload["network_reviewed"] is not True:
            raise ProjectValidationError(
                "Artwork review requires network_reviewed to be true."
            )
        if not self.server.metadata_network_lock.acquire(blocking=False):
            raise ProjectValidationError("Another metadata network action is already running.")
        normalized_artwork: dict[str, Any] | None = None
        try:
            with self.server.operation_lock:
                _album, state = _load_expected_album(
                    self.server.album_path,
                    expected_sha256=expected_album_sha256,
                    expected_revision=expected_album_revision,
                    recognition_provider=self.server.recognition_provider,
                )
                _assert_expected_sides(state, expected_sides)
                _assert_no_stale_sources(state)
                _proposal, _candidate, _entry, binding = (
                    _current_identification_candidate(
                        self.server.album_path,
                        state,
                        filename=filename,
                        file_sha256=file_sha256,
                        proposal_sha256=proposal_sha256,
                        release_mbid=release_mbid,
                    )
                )
                with self.server.release_review_lock:
                    release_review = copy.deepcopy(
                        self.server.release_reviews.get(release_review_sha256)
                    )
                if (
                    type(release_review) is not dict
                    or release_review.get("review_sha256") != release_review_sha256
                    or release_review.get("binding") != binding
                    or release_review.get("release", {}).get("release_mbid")
                    != release_mbid
                ):
                    raise ProjectValidationError(
                        "Fetch and review the exact current release details before artwork."
                    )
            try:
                raw_artwork = self.server.cover_art_client.download_front_art(
                    release_mbid,
                    size="1200",
                )
            except MetadataLookupError as exc:
                raise ProjectValidationError(
                    f"Cover Art Archive review failed: {str(exc)[:1_024]}"
                ) from exc
            normalized_artwork = _normalize_artwork_download(raw_artwork)
            artwork_path, artwork_bytes = _safe_review_artwork_path(
                self.server.album_path,
                relative_path=cast(str, normalized_artwork["relative_path"]),
                expected_sha256=cast(str, normalized_artwork["sha256"]),
                expected_size=cast(int, normalized_artwork["size_bytes"]),
            )
            mime_type = cast(str, normalized_artwork["mime_type"])
            valid_signature = (
                mime_type == "image/png"
                and artwork_bytes.startswith(b"\x89PNG\r\n\x1a\n")
            ) or (
                mime_type == "image/jpeg" and artwork_bytes.startswith(b"\xff\xd8\xff")
            )
            if not valid_signature:
                raise ProjectValidationError(
                    "Downloaded artwork bytes do not match the reviewed image type."
                )

            with self.server.operation_lock:
                _latest_album, latest_state = _load_expected_album(
                    self.server.album_path,
                    expected_sha256=expected_album_sha256,
                    expected_revision=expected_album_revision,
                    recognition_provider=self.server.recognition_provider,
                )
                _assert_expected_sides(latest_state, expected_sides)
                _assert_no_stale_sources(latest_state)
                _p, _c, _e, repeated_binding = _current_identification_candidate(
                    self.server.album_path,
                    latest_state,
                    filename=filename,
                    file_sha256=file_sha256,
                    proposal_sha256=proposal_sha256,
                    release_mbid=release_mbid,
                )
                if repeated_binding != binding:
                    raise _AlbumConflictError(
                        "The album release candidate changed during artwork download. Reload."
                    )

            artwork_binding = {
                **binding,
                "release_sha256": release_review["release_sha256"],
                "release_review_sha256": release_review_sha256,
            }
            preview_token = canonical_json_sha256(
                {
                    "binding": artwork_binding,
                    "artwork_sha256": normalized_artwork["sha256"],
                    "artwork_size_bytes": normalized_artwork["size_bytes"],
                }
            )
            reviewed_artwork = {
                **normalized_artwork,
                "preview_url": (
                    "/api/album/identification/artwork-preview/" + preview_token
                ),
            }
            artwork_review_body: dict[str, Any] = {
                "schema": _ALBUM_ARTWORK_REVIEW_SCHEMA,
                "binding": artwork_binding,
                "artwork": reviewed_artwork,
                "authority": _release_review_authority(artwork_downloaded=True),
            }
            artwork_review = dict(artwork_review_body)
            artwork_review["review_sha256"] = canonical_json_sha256(
                artwork_review_body
            )
            preview = _AlbumArtworkPreview(
                path=artwork_path,
                relative_path=cast(str, normalized_artwork["relative_path"]),
                sha256=cast(str, normalized_artwork["sha256"]),
                size_bytes=cast(int, normalized_artwork["size_bytes"]),
                mime_type=mime_type,
                album_sha256=expected_album_sha256,
                album_revision=expected_album_revision,
                proposal_filename=filename,
                proposal_file_sha256=file_sha256,
                proposal_sha256=proposal_sha256,
                release_mbid=release_mbid,
                release_sha256=cast(str, release_review["release_sha256"]),
            )
            with self.server.release_review_lock:
                self.server.artwork_previews[preview_token] = preview
        except BaseException:
            if normalized_artwork is not None:
                _discard_exact_review_artwork(
                    self.server.album_path,
                    normalized_artwork,
                )
            raise
        finally:
            self.server.metadata_network_lock.release()
        self._json(
            {
                "ok": True,
                "network_request_performed": True,
                "artwork": artwork_review,
                "state": latest_state,
            },
            status=HTTPStatus.CREATED,
        )

    def _create_publication_plan(self) -> None:
        payload = self._read_json()
        _strict_object(
            payload,
            fields=frozenset(
                {
                    "expected_album_sha256",
                    "expected_album_revision",
                    "expected_sides",
                    "action",
                    "reviewed",
                    "plan_filename",
                    "selected_profiles",
                    "restoration_mode",
                    "flac_compression",
                    "aac_bitrate_kbps",
                }
            ),
            label="Create-publication-plan request",
        )
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        expected_sides = _expected_sides(payload["expected_sides"])
        if payload["action"] != "create-reviewed-publication-plan":
            raise ProjectValidationError(
                "Publication planning requires the exact reviewed create-plan action."
            )
        if payload["reviewed"] is not True:
            raise ProjectValidationError(
                "Publication planning requires reviewed to be the JSON boolean true."
            )
        plan_filename = _strict_publication_plan_filename(payload["plan_filename"])
        selected_profiles = _strict_publication_profiles(
            payload["selected_profiles"]
        )
        restoration_mode = payload["restoration_mode"]
        if not isinstance(restoration_mode, str) or restoration_mode not in {
            "none",
            "reviewed",
        }:
            raise ProjectValidationError(
                "Restoration mode must be exactly 'none' or 'reviewed'."
            )
        flac_compression = _strict_publication_integer(
            payload["flac_compression"],
            "FLAC compression",
            minimum=0,
            maximum=12,
        )
        aac_bitrate_kbps = _strict_publication_integer(
            payload["aac_bitrate_kbps"],
            "AAC bitrate",
            minimum=64,
            maximum=512,
        )

        with self.server.operation_lock:
            _album, state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(state, expected_sides)
            _assert_no_stale_sources(state)
            publication = state.get("publication")
            readiness = (
                publication.get("readiness")
                if type(publication) is dict
                else None
            )
            if (
                type(readiness) is not dict
                or readiness.get("can_create_plan") is not True
            ):
                raise ProjectValidationError(
                    "The current album state is not ready to create a publication plan."
                )
            plan_path = self.server.album_path.parent / plan_filename
            plan = build_album_publication_plan(
                self.server.album_path,
                plan_path,
                selected_profiles=selected_profiles,
                restoration_mode=restoration_mode,
                flac_compression=flac_compression,
                aac_bitrate_kbps=aac_bitrate_kbps,
            )
            _latest_album, latest_state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(latest_state, expected_sides)
            entry = _current_publication_plan_entry(
                latest_state,
                plan.plan_sha256,
            )
            if entry.get("filename") != plan_filename:
                raise _AlbumConflictError(
                    "The created publication plan was not rediscovered exactly. Reload."
                )
        self._json(
            {
                "ok": True,
                "created_plan": {
                    "filename": plan_filename,
                    "plan_sha256": plan.plan_sha256,
                    "selected_profiles": list(plan.selected_profiles),
                    "restoration_mode": restoration_mode,
                },
                "state": latest_state,
            },
            status=HTTPStatus.CREATED,
        )

    def _preflight_publication_plan(self) -> None:
        payload = self._read_json()
        _strict_object(
            payload,
            fields=frozenset(
                {
                    "expected_album_sha256",
                    "expected_album_revision",
                    "expected_sides",
                    "action",
                    "plan_sha256",
                }
            ),
            label="Publication-preflight request",
        )
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        expected_sides = _expected_sides(payload["expected_sides"])
        if payload["action"] != "preflight-current-publication-plan":
            raise ProjectValidationError(
                "Publication preflight requires the exact current-plan action."
            )
        plan_sha256 = _strict_digest(
            payload["plan_sha256"], "Publication plan SHA-256"
        )

        with self.server.operation_lock:
            _album, state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(state, expected_sides)
            entry = _current_publication_plan_entry(state, plan_sha256)
            filename = cast(str, entry["filename"])
            expected_file_sha256 = entry.get("file_sha256")
            if not isinstance(expected_file_sha256, str):
                raise RuntimeError("Current publication plan has no file identity.")
            try:
                repeated_plan, repeated_file_sha256 = (
                    load_album_publication_plan_with_sha256(
                        self.server.album_path.parent / filename
                    )
                )
            except (ProjectValidationError, OSError) as exc:
                raise _AlbumConflictError(
                    "The publication plan changed after its live preflight. Reload."
                ) from exc
            if (
                repeated_plan.plan_sha256 != plan_sha256
                or repeated_file_sha256 != expected_file_sha256
                or _album_digest_or_conflict(self.server.album_path)
                != expected_album_sha256
            ):
                raise _AlbumConflictError(
                    "The publication plan changed during preflight. Reload."
                )
            selected_profiles = entry.get("selected_profiles")
            side_count = entry.get("side_count")
            if (
                type(selected_profiles) is not list
                or not all(isinstance(item, str) for item in selected_profiles)
                or type(side_count) is not int
                or side_count < 1
            ):
                raise RuntimeError("Current publication catalog entry is incomplete.")
        self._json(
            {
                "ok": True,
                "preflight": {
                    "filename": filename,
                    "plan_sha256": plan_sha256,
                    "album_sha256": expected_album_sha256,
                    "selected_profiles": selected_profiles,
                    "side_count": side_count,
                },
                "state": state,
            }
        )

    def _execute_publication_plan(self) -> None:
        payload = self._read_json()
        _strict_object(
            payload,
            fields=frozenset(
                {
                    "expected_album_sha256",
                    "expected_album_revision",
                    "expected_sides",
                    "action",
                    "owner_confirmed",
                    "confirmation",
                    "plan_sha256",
                    "plan_file_sha256",
                    "destination_name",
                }
            ),
            label="Execute-publication request",
        )
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        expected_sides = _expected_sides(payload["expected_sides"])
        plan_sha256 = _strict_digest(
            payload["plan_sha256"], "Publication plan SHA-256"
        )
        plan_file_sha256 = _strict_digest(
            payload["plan_file_sha256"], "Publication plan file SHA-256"
        )
        destination_name = _strict_publication_destination_name(
            payload["destination_name"]
        )
        if payload["action"] != "execute-current-publication-plan":
            raise ProjectValidationError(
                "Publication execution requires the exact current-plan action."
            )
        if payload["owner_confirmed"] is not True:
            raise ProjectValidationError(
                "Publication execution requires explicit owner confirmation."
            )
        if payload["confirmation"] != f"PUBLISH {destination_name}":
            raise ProjectValidationError(
                f"Publication execution requires typing PUBLISH {destination_name}."
            )

        progress, progress_callback = _progress_capture()
        with self.server.operation_lock:
            _album, state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(state, expected_sides)
            readiness = cast(dict[str, Any], state["publication"])["readiness"]
            if (
                type(readiness) is not dict
                or readiness.get("can_execute_current_plan") is not True
            ):
                raise ProjectValidationError(
                    "The current album state is not ready for publication execution."
                )
            entry = _current_publication_plan_entry(state, plan_sha256)
            if entry.get("file_sha256") != plan_file_sha256:
                raise _AlbumConflictError(
                    "The publication plan file identity changed. Reload."
                )
            filename = cast(str, entry["filename"])
            plan_path = self.server.album_path.parent / filename
            output_path = _assert_new_publication_destination(
                self.server.album_path,
                destination_name,
            )
            try:
                preflight = preflight_album_publication_plan(plan_path)
            except (ExportError, ProjectValidationError, OSError) as exc:
                raise _AlbumConflictError(
                    "The publication plan failed its immediate live preflight. Reload."
                ) from exc
            try:
                repeated_plan, repeated_file_sha256 = (
                    load_album_publication_plan_with_sha256(plan_path)
                )
            except (ProjectValidationError, OSError) as exc:
                raise _AlbumConflictError(
                    "The publication plan changed after preflight. Reload."
                ) from exc
            if (
                preflight.plan_sha256 != plan_sha256
                or preflight.album_sha256 != expected_album_sha256
                or repeated_plan.plan_sha256 != plan_sha256
                or repeated_file_sha256 != plan_file_sha256
                or _album_digest_or_conflict(self.server.album_path)
                != expected_album_sha256
            ):
                raise _AlbumConflictError(
                    "The album or publication plan changed after preflight. Reload."
                )
            try:
                execution = execute_album_publication_plan(
                    plan_path,
                    output_path,
                    progress=progress_callback,
                )
            except (ExportError, ProjectValidationError, OSError) as exc:
                raise ProjectValidationError(
                    f"Publication execution did not commit: {str(exc)[:1_024]}"
                ) from exc
            verification = verify_album_publication(output_path)
            _latest_album, latest_state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(latest_state, expected_sides)
            rediscovered = False
            if (
                verification.ok
                and verification.manifest_sha256 is not None
                and verification.journal_sha256 is not None
            ):
                try:
                    _publication_receipt_entry(
                        latest_state,
                        directory_name=destination_name,
                        manifest_sha256=verification.manifest_sha256,
                        journal_sha256=verification.journal_sha256,
                        plan_sha256=plan_sha256,
                        allowed_statuses=frozenset({"current"}),
                    )
                except _AlbumConflictError:
                    rediscovered = False
                else:
                    rediscovered = True
            verified = bool(verification.ok and rediscovered)
        self._json(
            {
                "ok": verified,
                "completion": "verified" if verified else "verification-failed",
                "destination_name": destination_name,
                "plan_sha256": plan_sha256,
                "plan_file_sha256": plan_file_sha256,
                "preflight": {
                    "album_sha256": preflight.album_sha256,
                    "plan_sha256": preflight.plan_sha256,
                    "selected_profiles": list(preflight.selected_profiles),
                    "side_count": preflight.side_count,
                },
                "execution": {
                    "plan_sha256": execution.plan_sha256,
                    "artifact_count": len(execution.artifacts),
                },
                "verification": _verification_payload(verification),
                "restart_rediscovered": rediscovered,
                "progress": progress,
                "state": latest_state,
            },
            status=HTTPStatus.CREATED if verified else HTTPStatus.OK,
        )

    def _verify_publication_receipt(self) -> None:
        payload = self._read_json()
        _strict_object(
            payload,
            fields=frozenset(
                {
                    "expected_album_sha256",
                    "expected_album_revision",
                    "expected_sides",
                    "action",
                    "directory_name",
                    "manifest_sha256",
                    "journal_sha256",
                    "plan_sha256",
                }
            ),
            label="Verify-publication request",
        )
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        expected_sides = _expected_sides(payload["expected_sides"])
        directory_name = _strict_publication_destination_name(
            payload["directory_name"]
        )
        manifest_sha256 = _strict_digest(
            payload["manifest_sha256"], "Publication manifest SHA-256"
        )
        journal_sha256 = _strict_digest(
            payload["journal_sha256"], "Publication journal SHA-256"
        )
        plan_sha256 = _strict_digest(
            payload["plan_sha256"], "Publication plan SHA-256"
        )
        if payload["action"] != "verify-discovered-publication":
            raise ProjectValidationError(
                "Publication verification requires the exact read-only action."
            )

        with self.server.operation_lock:
            _album, state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(state, expected_sides)
            _publication_receipt_entry(
                state,
                directory_name=directory_name,
                manifest_sha256=manifest_sha256,
                journal_sha256=journal_sha256,
                plan_sha256=plan_sha256,
                allowed_statuses=frozenset({"current", "stale"}),
            )
            verification = verify_album_publication(
                self.server.album_path.parent / directory_name
            )
            _latest_album, latest_state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(latest_state, expected_sides)
            exact = bool(
                verification.ok
                and verification.manifest_sha256 == manifest_sha256
                and verification.journal_sha256 == journal_sha256
            )
        self._json(
            {
                "ok": exact,
                "read_only": True,
                "verification": _verification_payload(verification),
                "state": latest_state,
            }
        )

    def _replay_publication_receipt(self) -> None:
        payload = self._read_json()
        _strict_object(
            payload,
            fields=frozenset(
                {
                    "expected_album_sha256",
                    "expected_album_revision",
                    "expected_sides",
                    "action",
                    "owner_confirmed",
                    "confirmation",
                    "plan_sha256",
                    "plan_file_sha256",
                    "source_directory_name",
                    "source_manifest_sha256",
                    "source_journal_sha256",
                    "destination_name",
                }
            ),
            label="Replay-publication request",
        )
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        expected_sides = _expected_sides(payload["expected_sides"])
        plan_sha256 = _strict_digest(
            payload["plan_sha256"], "Publication plan SHA-256"
        )
        plan_file_sha256 = _strict_digest(
            payload["plan_file_sha256"], "Publication plan file SHA-256"
        )
        source_name = _strict_publication_destination_name(
            payload["source_directory_name"]
        )
        source_manifest_sha256 = _strict_digest(
            payload["source_manifest_sha256"], "Source manifest SHA-256"
        )
        source_journal_sha256 = _strict_digest(
            payload["source_journal_sha256"], "Source journal SHA-256"
        )
        destination_name = _strict_publication_destination_name(
            payload["destination_name"]
        )
        if payload["action"] != "replay-current-publication":
            raise ProjectValidationError(
                "Publication replay requires the exact deliberate replay action."
            )
        if payload["owner_confirmed"] is not True:
            raise ProjectValidationError(
                "Publication replay requires explicit owner confirmation."
            )
        expected_confirmation = f"REPLAY {source_name} TO {destination_name}"
        if payload["confirmation"] != expected_confirmation:
            raise ProjectValidationError(
                f"Publication replay requires typing {expected_confirmation}."
            )

        progress, progress_callback = _progress_capture()
        with self.server.operation_lock:
            _album, state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(state, expected_sides)
            plan_entry = _current_publication_plan_entry(state, plan_sha256)
            if plan_entry.get("file_sha256") != plan_file_sha256:
                raise _AlbumConflictError(
                    "The replay plan file identity changed. Reload."
                )
            source_entry = _publication_receipt_entry(
                state,
                directory_name=source_name,
                manifest_sha256=source_manifest_sha256,
                journal_sha256=source_journal_sha256,
                plan_sha256=plan_sha256,
                allowed_statuses=frozenset({"current"}),
            )
            if source_entry.get("plan_file_sha256") != plan_file_sha256:
                raise _AlbumConflictError(
                    "The source publication and current sibling plan differ. Reload."
                )
            plan_path = self.server.album_path.parent / cast(
                str,
                plan_entry["filename"],
            )
            output_path = _assert_new_publication_destination(
                self.server.album_path,
                destination_name,
            )
            try:
                preflight = preflight_album_publication_plan(plan_path)
            except (ExportError, ProjectValidationError, OSError) as exc:
                raise _AlbumConflictError(
                    "The replay plan failed its immediate live preflight. Reload."
                ) from exc
            if (
                preflight.plan_sha256 != plan_sha256
                or preflight.album_sha256 != expected_album_sha256
                or _album_digest_or_conflict(self.server.album_path)
                != expected_album_sha256
            ):
                raise _AlbumConflictError(
                    "The album or replay plan changed after preflight. Reload."
                )
            try:
                replay = replay_album_publication(
                    self.server.album_path.parent / source_name,
                    output_path,
                    plan_path=plan_path,
                    progress=progress_callback,
                )
            except (ExportError, ProjectValidationError, OSError) as exc:
                raise ProjectValidationError(
                    f"Publication replay did not commit: {str(exc)[:1_024]}"
                ) from exc
            verification = verify_album_publication(output_path)
            _latest_album, latest_state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(latest_state, expected_sides)
            rediscovered = False
            if (
                verification.ok
                and verification.manifest_sha256 is not None
                and verification.journal_sha256 is not None
            ):
                try:
                    _publication_receipt_entry(
                        latest_state,
                        directory_name=destination_name,
                        manifest_sha256=verification.manifest_sha256,
                        journal_sha256=verification.journal_sha256,
                        plan_sha256=plan_sha256,
                        allowed_statuses=frozenset({"current"}),
                    )
                except _AlbumConflictError:
                    rediscovered = False
                else:
                    rediscovered = True
            replay_verified = bool(replay.ok and verification.ok and rediscovered)
        self._json(
            {
                "ok": replay_verified,
                "completion": "verified-match" if replay_verified else "mismatch",
                "source_directory_name": source_name,
                "destination_name": destination_name,
                "plan_sha256": plan_sha256,
                "plan_file_sha256": plan_file_sha256,
                "replay": {
                    "ok": replay.ok,
                    "mismatches": [
                        {
                            "code": mismatch.code,
                            "path": mismatch.path,
                            "expected": mismatch.expected,
                            "current": mismatch.current,
                            "message": mismatch.message,
                        }
                        for mismatch in replay.mismatches
                    ],
                },
                "verification": _verification_payload(verification),
                "restart_rediscovered": rediscovered,
                "progress": progress,
                "state": latest_state,
            },
            status=HTTPStatus.CREATED if replay_verified else HTTPStatus.OK,
        )

    def _recover_publication_orphan(self) -> None:
        payload = self._read_json()
        _strict_object(
            payload,
            fields=frozenset(
                {
                    "expected_album_sha256",
                    "expected_album_revision",
                    "expected_sides",
                    "action",
                    "owner_confirmed",
                    "confirmation",
                    "recovery_action",
                    "orphan_directory_name",
                    "orphan_kind",
                    "plan_sha256",
                    "journal_sha256",
                    "directory_identity",
                }
            ),
            label="Recover-publication-orphan request",
        )
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        expected_sides = _expected_sides(payload["expected_sides"])
        orphan_name = _strict_orphan_directory_name(
            payload["orphan_directory_name"]
        )
        orphan_kind = payload["orphan_kind"]
        if orphan_kind not in {"partial", "quarantine"}:
            raise ProjectValidationError(
                "Publication orphan kind must be exactly partial or quarantine."
            )
        plan_sha256 = _strict_digest(
            payload["plan_sha256"], "Orphan plan SHA-256"
        )
        journal_sha256 = _strict_digest(
            payload["journal_sha256"], "Orphan journal SHA-256"
        )
        identity = _strict_recovery_identity(payload["directory_identity"])
        recovery_action = payload["recovery_action"]
        if recovery_action not in {"quarantine", "remove"}:
            raise ProjectValidationError(
                "Recovery action must be exactly quarantine or remove."
            )
        if payload["action"] != "recover-owned-publication-orphan":
            raise ProjectValidationError(
                "Publication recovery requires the exact owned-orphan action."
            )
        if payload["owner_confirmed"] is not True:
            raise ProjectValidationError(
                "Publication recovery requires explicit owner confirmation."
            )
        expected_confirmation = (
            f"QUARANTINE {orphan_name}"
            if recovery_action == "quarantine"
            else f"REMOVE OWNED ORPHAN {orphan_name} {journal_sha256}"
        )
        if payload["confirmation"] != expected_confirmation:
            raise ProjectValidationError(
                f"Publication recovery requires typing {expected_confirmation}."
            )

        with self.server.operation_lock:
            _album, state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(state, expected_sides)
            operations = _publication_operations(state)
            if operations.get("scan_complete") is not True:
                raise ProjectValidationError(
                    "Publication orphan inventory is incomplete; recovery is disabled."
                )
            _publication_orphan_entry(
                state,
                directory_name=orphan_name,
                kind=cast(str, orphan_kind),
                plan_sha256=plan_sha256,
                journal_sha256=journal_sha256,
                identity=identity,
            )
            try:
                recovery = recover_album_publication_orphan(
                    self.server.album_path.parent / orphan_name,
                    expected_identity=identity,
                    expected_journal_sha256=journal_sha256,
                    action=cast(Any, recovery_action),
                )
            except (ExportError, OSError) as exc:
                raise _AlbumConflictError(
                    "The publication orphan changed before recovery. Reload."
                ) from exc
            _latest_album, latest_state = _load_expected_album(
                self.server.album_path,
                expected_sha256=expected_album_sha256,
                expected_revision=expected_album_revision,
                recognition_provider=self.server.recognition_provider,
            )
            _assert_expected_sides(latest_state, expected_sides)
        self._json(
            {
                "ok": True,
                "recovery": {
                    "action": recovery.action,
                    "original_directory_name": Path(recovery.original_path).name,
                    "resulting_directory_name": (
                        None
                        if recovery.resulting_path is None
                        else Path(recovery.resulting_path).name
                    ),
                    "removed": recovery.removed,
                },
                "state": latest_state,
            }
        )

    def _open_side(self) -> None:
        payload = self._read_json()
        fields = frozenset(
            {
                "expected_album_sha256",
                "expected_album_revision",
                "side_label",
                "expected_current_identity",
            }
        )
        _strict_object(payload, fields=fields, label="Open-side request")
        expected_album_sha256 = _strict_digest(
            payload["expected_album_sha256"], "Expected album SHA-256"
        )
        expected_album_revision = _strict_album_revision(
            payload["expected_album_revision"]
        )
        side_label = _strict_side_label(payload["side_label"])
        expected_identity = _expected_identity(payload["expected_current_identity"])

        with self.server.operation_lock:
            _assert_fixed_album_destination(self.server.album_path)
            current_album_sha256 = _album_digest_or_conflict(self.server.album_path)
            if current_album_sha256 != expected_album_sha256:
                raise _AlbumConflictError(
                    "The album project changed after this review page loaded. Reload."
                )
            try:
                album, album_sha256 = load_album_project_with_sha256(
                    self.server.album_path
                )
                state = _workbench_state(
                    album,
                    self.server.album_path,
                    album_sha256,
                    self.server.recognition_provider,
                )
            except ProjectValidationError as exc:
                raise _AlbumConflictError(
                    "An album side changed while its review state was loaded. Reload."
                ) from exc
            if album_sha256 != expected_album_sha256:
                raise _AlbumConflictError(
                    "The album project changed after this review page loaded. Reload."
                )
            if album.revision != expected_album_revision:
                raise _AlbumConflictError(
                    "The album revision changed after this review page loaded. Reload."
                )

            current_identity = _side_current_identity(state, side_label)
            self.server.retire_stale_side_review(side_label, current_identity)
            if current_identity != expected_identity:
                raise _AlbumConflictError(
                    f"Side {side_label} changed after it was loaded. Reload."
                )
            project_path = _resolved_side_project(state, side_label)
            child, reused = self.server.open_side_review(
                side_label,
                project_path,
                current_identity,
            )

            try:
                latest_album, latest_album_sha256 = load_album_project_with_sha256(
                    self.server.album_path
                )
                latest_state = _workbench_state(
                    latest_album,
                    self.server.album_path,
                    latest_album_sha256,
                    self.server.recognition_provider,
                )
                latest_identity = _side_current_identity(latest_state, side_label)
            except (OSError, ProjectValidationError):
                self.server.retire_side_review(side_label)
                raise _AlbumConflictError(
                    f"Side {side_label} changed while its review was opening. Reload."
                ) from None
            if latest_identity != current_identity:
                self.server.retire_side_review(side_label)
                raise _AlbumConflictError(
                    f"Side {side_label} changed while its review was opening. Reload."
                )
            if (
                latest_album_sha256 != expected_album_sha256
                or latest_album.revision != expected_album_revision
                or _album_digest_or_conflict(self.server.album_path)
                != expected_album_sha256
            ):
                raise _AlbumConflictError(
                    "The album project changed while its side review was opening. Reload."
                )

        self._json(
            {
                "ok": True,
                "url": child.url,
                "side_label": side_label,
                "current_identity": current_identity,
                "reused": reused,
            }
        )


def _ipv4_server_endpoint(server: AlbumReviewServer) -> tuple[str, int]:
    address = server.server_address
    if server.address_family != socket.AF_INET or not isinstance(address, tuple):
        raise GrooveSerpentError("The album review server did not bind an IPv4 endpoint.")
    if len(address) != 2:
        raise GrooveSerpentError("The album review server returned an invalid endpoint.")
    host, port = address
    if not isinstance(host, str) or type(port) is not int:
        raise GrooveSerpentError("The album review server returned an invalid endpoint.")
    return host, port


def serve_album_project(
    album_path: Path,
    *,
    port: int = 0,
    open_browser: bool = True,
) -> int:
    """Serve one validated album project on an ephemeral loopback endpoint."""

    if type(port) is not int or not 0 <= port <= 65_535:
        raise ProjectValidationError(
            "The album review port must be a JSON integer from 0 to 65535."
        )
    album_path = canonical_album_path(album_path)
    load_album_project(album_path)
    server = AlbumReviewServer(("127.0.0.1", port), album_path)
    _host, selected_port = _ipv4_server_endpoint(server)
    url = f"{server.session_auth.origin(port=selected_port)}/"
    bootstrap_url = server.session_auth.bootstrap_url(port=selected_port)
    print(f"Reviewing album {album_path.name}")
    print(f"Local Album Workbench: {url}")
    if not open_browser:
        print(
            "One-time session bootstrap URL (keep this credential private): "
            f"{bootstrap_url}"
        )
    print("Press Ctrl+C to stop the album review server.")
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(bootstrap_url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nAlbum review server stopped.")
    finally:
        server.server_close()
    return 0
