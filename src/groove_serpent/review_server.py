from __future__ import annotations

import ipaddress
import hashlib
import json
import mimetypes
import os
import re
import shutil
import socket
import sys
import threading
import uuid
import webbrowser
from dataclasses import dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, BinaryIO, Mapping, TypedDict, cast
from urllib.parse import urlparse, urlsplit

from . import __version__, endpoint_proposals as endpoint_proposal_module
from .album import project_speed_state
from .album_publication_policy import speed_correction_details
from .audio_snapshot import VerifiedAudioSnapshot, verified_audio_snapshot
from .cache_storage import resolve_cache_root
from .continuous_preview_workflow import (
    ReviewedNoiseReference,
    current_continuous_preview_context,
    discover_continuous_preview_catalog,
    find_current_continuous_artifact,
    propose_continuous_preview,
    reject_continuous_proposal,
    render_continuous_preview,
    validate_continuous_attestation,
)
from .errors import GrooveSerpentError, ProjectValidationError
from .evidence import (
    EvidenceCache,
    EvidenceRequestSuperseded,
    MAX_EVIDENCE_SECONDS,
    analyze_evidence_window,
    evidence_cache_key,
)
from .endpoint_proposals import (
    EndpointProposalConfig,
    EndpointScope,
    analyze_endpoint_proposals,
    load_endpoint_proposal_document,
    validate_endpoint_proposal_document,
)
from .exporter import export_project, suggest_output_directory
from .metadata import (
    CoverArtArchiveClient,
    MetadataLookupError,
    MusicBrainzClient,
    find_track_selections,
)
from .media import sha256_file
from .models import AudioSource, Project, ProjectState, Track, resolve_source_path
from .project_io import load_project, load_project_with_sha256, save_project
from .publication import FileReceipt, same_file_object_stats
from .recognition import (
    RECOGNITION_SPEED_TRANSFORM,
    AcoustIDRecognitionProvider,
    RecognitionProvider,
)
from .restoration_catalog import (
    RestorationArtifact,
    RestorationCatalog,
    discover_restoration_catalog,
)
from .restoration_workflow import (
    MAX_PREVIEW_CANDIDATES,
    PREVIEW_SCHEMA,
    RECIPE_SCHEMA,
    RENDER_SCHEMA,
    SCAN_SCHEMA,
    create_click_preview,
    create_restoration_recipe,
    render_restored_side,
    scan_project_clicks,
)
from .session_auth import (
    LoopbackSessionAuth,
    SessionAuthentication,
    request_target_is_exact,
)
from .topology import propose_topology_refit, tracks_from_topology_proposal
from .validation import strict_finite_number


_MAX_REQUEST_BODY = 2_000_000
_RESTORATION_WORKSPACE_DIR = ".groove-serpent"
_RESTORATION_PROTECTED_CLASSIFICATIONS = {
    "needle-drop",
    "needle-pickup",
    "handling-event",
    "other-structural-event",
}
_RESTORATION_DECISIONS = {"approved", "rejected", "protected"}
_SAVE_TRACK_FIELDS = {
    "number",
    "title",
    "start_sample",
    "end_sample",
    "start_seconds",
    "end_seconds",
    "confidence",
    "artist",
    "album",
    "album_artist",
    "year",
    "genre",
    "side",
    "expected_duration_seconds",
    "musicbrainz_recording_id",
    "musicbrainz_track_id",
}
_AAC_BITRATE_PATTERN = re.compile(r"([1-9][0-9]{1,2})k")
_ENDPOINT_REVIEW_INTENT = "end-at-wanted-music-remove-lead-in-and-runout"


class _ProjectConflictError(GrooveSerpentError):
    """A browser tried to mutate a project version it did not load."""


class _SourceFileIdentity(TypedDict):
    device: int
    inode: int
    change_ns: int
    birth_ns: int | None


class _SourceReceipt(TypedDict):
    receipt: str
    sha256: str
    size_bytes: int
    modified_ns: int
    file_identity: _SourceFileIdentity


_StatIdentity = tuple[int, int, int, int, int, int | None, int | None]
_SourceCacheKey = tuple[
    str,
    int,
    int,
    int,
    int,
    int,
    int | None,
    int | None,
    str,
]


@dataclass(frozen=True, slots=True)
class _EvidenceRequestLease:
    generation: int
    cancelled_event: threading.Event

    def cancelled(self) -> bool:
        return self.cancelled_event.is_set()


def _project_payload(
    project: Project,
    project_path: Path,
    project_sha256: str,
    source_receipt: _SourceReceipt,
) -> dict[str, Any]:
    payload = project.to_dict()
    payload["default_output_dir"] = str(suggest_output_directory(project, project_path))
    payload["project_sha256"] = project_sha256
    payload["source_receipt"] = source_receipt
    return payload


def _expected_project_state(payload: dict[str, Any]) -> tuple[int, str]:
    revision = payload.get("expected_revision")
    if type(revision) is not int or revision <= 0:
        raise ProjectValidationError("Expected project revision must be a positive JSON integer.")
    digest = payload.get("expected_project_sha256")
    if not isinstance(digest, str):
        raise ProjectValidationError("Expected project SHA-256 must be text.")
    digest = digest.strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ProjectValidationError("Expected project SHA-256 is invalid.")
    return revision, digest


def _expected_source_receipt(payload: Mapping[str, Any]) -> str:
    receipt = payload.get("expected_source_receipt")
    if (
        not isinstance(receipt, str)
        or len(receipt) != 32
        or any(character not in "0123456789abcdef" for character in receipt)
    ):
        raise ProjectValidationError(
            "Expected source receipt must be the current 32-character session receipt."
        )
    return receipt


def _strict_aac_bitrate(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip().lower():
        raise ProjectValidationError("AAC bitrate must be lowercase text such as '256k'.")
    matched = _AAC_BITRATE_PATTERN.fullmatch(value)
    if matched is None or not 32 <= int(matched.group(1)) <= 512:
        raise ProjectValidationError("AAC bitrate must be an integer from 32k through 512k.")
    return value


def _stat_identity(value: os.stat_result) -> _StatIdentity:
    """Return stable platform identity and change fields without access time."""

    birth_ns = getattr(value, "st_birthtime_ns", None)
    file_attributes = getattr(value, "st_file_attributes", None)
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
        int(birth_ns) if birth_ns is not None else None,
        int(file_attributes) if file_attributes is not None else None,
    )


def _source_probe_signature(handle: BinaryIO, size: int) -> str:
    """Cheap cache discriminator; authoritative audio operations still full-hash."""

    digest = hashlib.blake2b(digest_size=16)
    block_size = 64 * 1024
    offsets = sorted(
        {
            0,
            max(0, size // 2 - block_size // 2),
            max(0, size - block_size),
        }
    )
    for offset in offsets:
        handle.seek(offset)
        data = handle.read(min(block_size, max(0, size - offset)))
        digest.update(offset.to_bytes(8, "big", signed=False))
        digest.update(len(data).to_bytes(8, "big", signed=False))
        digest.update(data)
    return digest.hexdigest()


def _sha256_handle(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    handle.seek(0)
    while chunk := handle.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _assert_project_state(expected: tuple[int, str], project: Project, project_sha256: str) -> None:
    expected_revision, expected_sha256 = expected
    if project.revision != expected_revision or project_sha256 != expected_sha256:
        raise _ProjectConflictError(
            "The project changed after this review page loaded. "
            "Reload before applying more changes."
        )


def _append_inferred_history(project: Project, before: ProjectState) -> bool:
    """Record one exact persisted transition for a browser save."""

    after = project.capture_state()
    if before.sha256 == after.sha256:
        project.validate()
        return False

    before_count = len(before.tracks)
    after_count = len(after.tracks)
    before_markers = (
        before.tracks[0].start_sample,
        *[track.end_sample for track in before.tracks],
    )
    after_markers = (
        after.tracks[0].start_sample,
        *[track.end_sample for track in after.tracks],
    )
    metadata_changed = before.metadata != after.metadata
    tracks_changed = before.tracks != after.tracks
    markers_changed = before_markers != after_markers

    reasons: list[str] = []
    if before_count != after_count:
        reasons.append(f"track count {before_count} → {after_count}")
    if markers_changed:
        reasons.append("sample markers")
    if tracks_changed and not markers_changed and before_count == after_count:
        reasons.append("track details")
    if metadata_changed:
        reasons.append("release or speed metadata")

    if after_count > before_count and not metadata_changed:
        action = "split_track"
    elif after_count < before_count and not metadata_changed:
        action = "merge_tracks"
    elif markers_changed and not metadata_changed and before_count == after_count:
        action = "move_marker"
    elif metadata_changed and not tracks_changed:
        action = "edit_metadata"
    elif tracks_changed and not markers_changed and not metadata_changed:
        action = "edit_track"
    else:
        action = "batch_edit"
    summary = "Saved " + ", ".join(reasons or ["project edits"])
    project.append_history(
        action=action,
        summary=summary[:512],
        before=before,
        after=after,
    )
    return True


def _optional_mbid(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProjectValidationError(f"{label} must be text.")
    rendered = value.strip()
    if not rendered:
        return ""
    try:
        return str(uuid.UUID(rendered))
    except ValueError as exc:
        raise ProjectValidationError(f"{label} must be a valid UUID.") from exc


def _loopback_addresses(host: str) -> list[tuple[int, str]]:
    """Resolve a permitted host without accepting a non-loopback result."""
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
        for family, _socket_type, _protocol, _canonical_name, sockaddr in resolved:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                return []
            value = sockaddr[0]
            try:
                resolved_address = ipaddress.ip_address(value)
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


def _strict_json_object(
    payload: Mapping[str, Any],
    *,
    allowed: set[str],
    required: set[str],
    label: str,
) -> None:
    unknown = set(payload) - allowed
    missing = required - set(payload)
    if unknown:
        raise ProjectValidationError(
            f"{label} contains unsupported fields: "
            + ", ".join(sorted(str(value) for value in unknown))
        )
    if missing:
        raise ProjectValidationError(
            f"{label} is missing required fields: "
            + ", ".join(sorted(str(value) for value in missing))
        )


def _finite_json_number(
    value: Any,
    *,
    label: str,
    minimum: float,
    maximum: float,
) -> float:
    rendered = strict_finite_number(value, label)
    if not minimum <= rendered <= maximum:
        raise ProjectValidationError(f"{label} must be between {minimum:g} and {maximum:g}.")
    return rendered


def _restoration_token(value: Any, prefix: str, label: str) -> str:
    if not isinstance(value, str):
        raise ProjectValidationError(f"{label} must be a session token.")
    expected_prefix = f"{prefix}-"
    suffix = value[len(expected_prefix) :] if value.startswith(expected_prefix) else ""
    if len(suffix) != 32 or any(character not in "0123456789abcdef" for character in suffix):
        raise ProjectValidationError(f"{label} is not a valid session token.")
    return value


def _continuous_digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProjectValidationError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _compact_restoration_candidate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProjectValidationError("The click scan contains an invalid candidate.")

    def invalid() -> ProjectValidationError:
        return ProjectValidationError("The click scan contains an invalid candidate.")

    candidate_id = raw.get("id")
    if (
        not isinstance(candidate_id, str)
        or not candidate_id.startswith("clk-")
        or len(candidate_id) > 160
    ):
        raise invalid()

    kind = raw.get("type")
    if not isinstance(kind, str) or kind not in {"impulse", "clipped"}:
        raise invalid()
    rendered_kind = kind

    confidence = raw.get("confidence")
    try:
        rendered_confidence = strict_finite_number(confidence, "Restoration candidate confidence")
    except ProjectValidationError:
        raise invalid() from None
    if not 0.0 <= rendered_confidence <= 1.0:
        raise invalid()

    start_frame = raw.get("start_frame")
    end_frame = raw.get("end_frame_exclusive")
    peak_frame = raw.get("peak_frame")
    if any(type(value) is not int for value in (start_frame, end_frame, peak_frame)):
        raise invalid()
    rendered_start_frame = cast(int, start_frame)
    rendered_end_frame = cast(int, end_frame)
    rendered_peak_frame = cast(int, peak_frame)
    if (
        rendered_start_frame < 0
        or rendered_end_frame <= rendered_start_frame
        or not rendered_start_frame <= rendered_peak_frame < rendered_end_frame
    ):
        raise invalid()

    start_seconds = raw.get("start_seconds")
    end_seconds = raw.get("end_seconds")
    try:
        rendered_start_seconds = strict_finite_number(start_seconds, "Restoration candidate start")
        rendered_end_seconds = strict_finite_number(end_seconds, "Restoration candidate end")
    except ProjectValidationError:
        raise invalid() from None
    if rendered_end_seconds <= rendered_start_seconds:
        raise invalid()

    channels = raw.get("channels")
    if type(channels) is not list or not channels:
        raise invalid()
    rendered_channels: list[int] = []
    for channel in cast(list[object], channels):
        if type(channel) is not int:
            raise invalid()
        rendered_channel = channel
        if rendered_channel < 0:
            raise invalid()
        rendered_channels.append(rendered_channel)
    if rendered_channels != sorted(set(rendered_channels)):
        raise invalid()

    repairable = raw.get("repairable")
    if type(repairable) is not bool:
        raise invalid()
    rendered_repairable = repairable

    compact = {
        "id": candidate_id,
        "type": rendered_kind,
        "confidence": rendered_confidence,
        "start_frame": rendered_start_frame,
        "end_frame_exclusive": rendered_end_frame,
        "peak_frame": rendered_peak_frame,
        "start_seconds": rendered_start_seconds,
        "end_seconds": rendered_end_seconds,
        "channels": rendered_channels,
        "repairable": rendered_repairable,
    }
    for key in ("classification", "morphology", "morphology_classification"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            compact[key] = value.strip()[:100]
    return compact


def _compact_restoration_scan(
    payload: Mapping[str, Any],
    *,
    token: str,
    digest: str,
) -> dict[str, Any]:
    if payload.get("schema") != SCAN_SCHEMA:
        raise ProjectValidationError("The click workflow returned an invalid scan schema.")
    candidates_raw = payload.get("candidates")
    summary = payload.get("summary")
    scan_range = payload.get("scan")
    if (
        type(candidates_raw) is not list
        or type(summary) is not dict
        or type(scan_range) is not dict
    ):
        raise ProjectValidationError("The click workflow returned an incomplete scan.")
    candidates = [_compact_restoration_candidate(item) for item in candidates_raw]
    identifiers = [item["id"] for item in candidates]
    if len(identifiers) != len(set(identifiers)):
        raise ProjectValidationError("The click workflow returned duplicate candidate IDs.")
    compact = {
        "token": token,
        "sha256": digest,
        "created_at": str(payload.get("created_at", ""))[:200],
        "range": dict(scan_range),
        "summary": dict(summary),
        "candidates": candidates,
    }
    coverage = payload.get("coverage")
    if isinstance(coverage, dict):
        compact["coverage"] = dict(coverage)
    return compact


def _read_restoration_json(path: Path, schema: str) -> dict[str, Any]:
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ProjectValidationError("A restoration artifact exceeds the 50 MB limit.")

    def reject_constant(value: str) -> None:
        raise ValueError(f"Invalid JSON number: {value}")

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProjectValidationError("A restoration artifact is invalid JSON.") from exc
    if type(payload) is not dict or payload.get("schema") != schema:
        raise ProjectValidationError(
            f"A restoration artifact does not use the expected {schema} schema."
        )
    return payload


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        project_path: Path,
        *,
        endpoint_proposal_path: Path | None = None,
    ):
        host, port = address
        loopback_addresses = _loopback_addresses(host)
        if not loopback_addresses:
            raise ValueError("Review server host must resolve only to loopback addresses.")
        family, resolved_host = next(
            (item for item in loopback_addresses if item[0] == socket.AF_INET),
            loopback_addresses[0],
        )
        self.project_path = project_path.expanduser().resolve()
        self.session_auth = LoopbackSessionAuth()
        project_root = self.project_path.parent.resolve()
        safe_stem = (
            "-".join(
                part
                for part in "".join(
                    character if character.isascii() and character.isalnum() else "-"
                    for character in self.project_path.stem
                ).split("-")
                if part
            )[:80]
            or "project"
        )
        self.restoration_workspace = (
            project_root / _RESTORATION_WORKSPACE_DIR / "restoration" / safe_stem
        )
        try:
            self.restoration_workspace.relative_to(project_root)
        except ValueError as exc:
            raise ProjectValidationError(
                "The restoration workspace must remain inside the project folder."
            ) from exc
        if self.restoration_workspace.resolve() != self.restoration_workspace:
            raise ProjectValidationError(
                "The restoration workspace may not traverse a symlink or reparse point."
            )
        self.restoration_artifacts: dict[str, dict[str, Any]] = {}
        self.restoration_audio: dict[str, dict[str, Any]] = {}
        self.latest_restoration_scan: str | None = None
        self.latest_restoration_recipe: str | None = None
        self.latest_restoration_preview: str | None = None
        self.latest_restoration_render: str | None = None
        self.restoration_catalog_diagnostics: dict[str, Any] = {
            "stale": {"count": 0, "by_kind": {}, "by_reason": {}},
            "invalid": {"count": 0, "by_kind": {}, "by_code": {}},
        }
        self._source_verification_cache: dict[_SourceCacheKey, tuple[str, str]] = {}
        self.evidence_cache = EvidenceCache()
        self._evidence_state_lock = threading.Lock()
        self._evidence_generation = 0
        self._active_evidence_request: _EvidenceRequestLease | None = None
        self.endpoint_proposal: dict[str, Any] | None = None
        self.endpoint_proposal_source_receipt: str | None = None
        project, project_sha256 = load_project_with_sha256(self.project_path)
        source = resolve_source_path(project, self.project_path).resolve()
        snapshot_workspace = resolve_cache_root(project_path=self.project_path)
        self.source_snapshot_workspace = snapshot_workspace
        self.source_snapshot = verified_audio_snapshot(
            source,
            expected_sha256=project.source.sha256,
            expected_size_bytes=project.source.size_bytes,
            workspace=snapshot_workspace,
            label="Source audio",
        )
        try:
            # The capture authenticates every source byte while copying.  Read the
            # completed snapshot once as well, at session startup, so later browser
            # range requests can rely exclusively on its lease and file identity.
            self.source_snapshot.assert_snapshot_unchanged(force=True)
            self._seed_source_verification_cache(project)
            _source, source_receipt = self.verify_source(project)
            catalog = discover_restoration_catalog(
                self.restoration_workspace,
                self.project_path,
                verified_source_sha256=self.source_snapshot.sha256,
            )
            self._restore_restoration_catalog(
                catalog,
                project,
                project_sha256,
                source_receipt,
            )
            if endpoint_proposal_path is not None:
                self._load_initial_endpoint_proposal(
                    endpoint_proposal_path,
                    project,
                    project_sha256,
                    source_receipt,
                )
        except BaseException:
            self.source_snapshot.close()
            raise
        self.address_family = family
        try:
            super().__init__((resolved_host, port), ReviewHandler)
        except BaseException:
            self.source_snapshot.close()
            raise
        self.operation_lock = threading.Lock()
        self.recognition_lock = threading.Lock()
        self.musicbrainz_client = MusicBrainzClient()
        self.cover_art_client = CoverArtArchiveClient(project_path.parent)
        self.recognition_provider: RecognitionProvider = AcoustIDRecognitionProvider()

    def _load_initial_endpoint_proposal(
        self,
        proposal_path: Path,
        project: Project,
        project_sha256: str,
        source_receipt: _SourceReceipt,
    ) -> None:
        """Load one exact actionable sealed proposal before opening the listener."""

        proposal = load_endpoint_proposal_document(proposal_path)
        expected_project = {
            "sha256": project_sha256,
            "revision": project.revision,
            "state_sha256": project.state_sha256,
        }
        expected_source = {
            "sha256": project.source.sha256,
            "size_bytes": project.source.size_bytes,
            "sample_rate": project.source.sample_rate,
            "channels": project.source.channels,
            "bits_per_raw_sample": project.source.bits_per_raw_sample,
            "sample_count": project.source.sample_count,
            "codec_name": project.source.codec_name,
        }
        if (
            proposal["project"] != expected_project
            or proposal["source"] != expected_source
        ):
            raise ProjectValidationError(
                "The sealed endpoint proposal is stale for this exact project or source."
            )
        module_path_value = endpoint_proposal_module.__file__
        if module_path_value is None:
            raise ProjectValidationError(
                "The current endpoint proposal module has no verifiable file identity."
            )
        algorithm = proposal["algorithm"]
        if (
            algorithm["module_sha256"] != sha256_file(Path(module_path_value))
            or algorithm["app_version"] != __version__
        ):
            raise ProjectValidationError(
                "The sealed endpoint proposal was created by different endpoint code."
            )
        expected_configuration = EndpointProposalConfig().to_dict()
        if proposal["configuration"]["values"] != expected_configuration:
            raise ProjectValidationError(
                "The sealed endpoint proposal uses a different review configuration."
            )
        scopes = proposal["scopes"]
        sample_count = project.source.sample_count
        if (
            len(scopes) != 1
            or scopes[0]["scope_start_sample"] != 0
            or scopes[0]["scope_end_sample_exclusive"] != sample_count
        ):
            raise ProjectValidationError(
                "Side review requires one full-source sealed endpoint scope."
            )
        if scopes[0]["status"] != "proposed":
            raise ProjectValidationError(
                "The sealed endpoint analysis abstained and cannot be loaded for acceptance."
            )
        self.endpoint_proposal = proposal
        self.endpoint_proposal_source_receipt = source_receipt["receipt"]

    def server_close(self) -> None:
        """Close the listener and remove the session-owned source snapshot."""

        try:
            super().server_close()
        finally:
            with self._evidence_state_lock:
                if self._active_evidence_request is not None:
                    self._active_evidence_request.cancelled_event.set()
                self._active_evidence_request = None
            self.evidence_cache.clear()
            self.source_snapshot.close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Keep ordinary browser disconnects out of the local review log."""

        if isinstance(
            sys.exception(),
            (BrokenPipeError, ConnectionAbortedError, ConnectionResetError),
        ):
            return
        super().handle_error(request, client_address)

    @staticmethod
    def _new_session_token(prefix: str, registry: Mapping[str, Any]) -> str:
        for _attempt in range(20):
            token = f"{prefix}-{uuid.uuid4().hex}"
            if token not in registry:
                return token
        raise GrooveSerpentError("Could not allocate a unique restoration session token.")

    @staticmethod
    def _stable_content_token(
        prefix: str,
        identity_sha256: str,
        registry: Mapping[str, Any],
    ) -> str:
        """Return a restart-stable token and fail closed on a prefix collision."""

        if len(identity_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in identity_sha256
        ):
            raise GrooveSerpentError("A restoration artifact has an invalid content identity.")
        token = f"{prefix}-{identity_sha256[:32]}"
        if token in registry:
            raise GrooveSerpentError(
                "An identical or colliding restoration artifact is already registered."
            )
        return token

    @classmethod
    def _stable_audio_token(
        cls,
        preview_token: str,
        role: str,
        file_sha256: str,
        registry: Mapping[str, Any],
    ) -> str:
        identity = hashlib.sha256(
            json.dumps(
                [preview_token, role, file_sha256],
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return cls._stable_content_token("audio", identity, registry)

    @staticmethod
    def _bounded_diagnostic_counts(
        labels: list[str],
        *,
        maximum_groups: int = 16,
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for label in labels:
            counts[label] = counts.get(label, 0) + 1
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        result = dict(ordered[:maximum_groups])
        omitted = sum(count for _label, count in ordered[maximum_groups:])
        if omitted:
            result["_other"] = omitted
        return result

    def _catalog_diagnostic_summary(
        self,
        catalog: RestorationCatalog,
    ) -> dict[str, Any]:
        stale_reasons = [reason for artifact in catalog.stale for reason in artifact.stale_reasons]
        return {
            "stale": {
                "count": len(catalog.stale),
                "by_kind": self._bounded_diagnostic_counts(
                    [artifact.kind for artifact in catalog.stale]
                ),
                "by_reason": self._bounded_diagnostic_counts(stale_reasons),
            },
            "invalid": {
                "count": len(catalog.invalid),
                "by_kind": self._bounded_diagnostic_counts(
                    [issue.kind or "workspace" for issue in catalog.invalid]
                ),
                "by_code": self._bounded_diagnostic_counts(
                    [issue.code for issue in catalog.invalid]
                ),
            },
        }

    @staticmethod
    def _catalog_entry_base(
        artifact: RestorationArtifact,
        project: Project,
        project_sha256: str,
        source_receipt: _SourceReceipt,
    ) -> dict[str, Any]:
        return {
            "kind": artifact.kind,
            "path": artifact.manifest_path,
            "sha256": artifact.manifest_sha256,
            "project_revision": project.revision,
            "project_sha256": project_sha256,
            "source_receipt": source_receipt["receipt"],
            "source_sha256": source_receipt["sha256"],
            "payload": artifact.payload,
        }

    def _restore_restoration_catalog(
        self,
        catalog: RestorationCatalog,
        project: Project,
        project_sha256: str,
        source_receipt: _SourceReceipt,
    ) -> None:
        """Rebuild only the catalog's current, fully verified artifact chain."""

        if (
            catalog.project_path != self.project_path
            or catalog.project_sha256 != project_sha256
            or catalog.source_sha256 != source_receipt["sha256"]
        ):
            raise ProjectValidationError(
                "The restoration catalog no longer matches this review session."
            )
        self.restoration_catalog_diagnostics = self._catalog_diagnostic_summary(catalog)
        self.restoration_artifacts.clear()
        self.restoration_audio.clear()
        for artifact in catalog.artifacts:
            token = artifact.artifact_id
            base = self._catalog_entry_base(
                artifact,
                project,
                project_sha256,
                source_receipt,
            )
            if artifact.kind == "scan":
                public = _compact_restoration_scan(
                    artifact.payload,
                    token=token,
                    digest=artifact.manifest_sha256,
                )
                base["public"] = public
                self.restoration_artifacts[token] = base
                continue

            dependency_tokens = {
                dependency.kind: dependency.artifact_id for dependency in artifact.dependencies
            }
            scan_token = dependency_tokens["scan"]
            scan_entry = self.restoration_artifacts.get(scan_token)
            if scan_entry is None:
                raise ProjectValidationError(
                    "A current restoration artifact has no registered scan."
                )
            if artifact.kind == "recipe":
                decisions = [
                    dict(item) for item in cast(list[dict[str, Any]], artifact.payload["decisions"])
                ]
                public = {
                    "token": token,
                    "sha256": artifact.manifest_sha256,
                    "scan_token": scan_token,
                    "created_at": artifact.created_at,
                    "summary": dict(cast(dict[str, Any], artifact.payload["summary"])),
                    "decisions": decisions,
                    "coverage": dict(cast(dict[str, Any], artifact.payload["coverage"])),
                }
                base.update({"scan_token": scan_token, "public": public})
                self.restoration_artifacts[token] = base
                continue

            if artifact.kind == "preview":
                compact_scan_candidates = {
                    item["id"]: item
                    for item in cast(
                        list[dict[str, Any]],
                        cast(dict[str, Any], scan_entry["public"])["candidates"],
                    )
                }
                selected = cast(list[dict[str, Any]], artifact.payload["candidates"])
                selected_public = [
                    compact_scan_candidates[cast(str, item["id"])] for item in selected
                ]
                audio_public: dict[str, dict[str, Any]] = {}
                for output in artifact.files:
                    audio_token = self._stable_audio_token(
                        token,
                        output.role,
                        output.sha256,
                        self.restoration_audio,
                    )
                    self.restoration_audio[audio_token] = {
                        "role": output.role,
                        "path": output.path,
                        "sha256": output.sha256,
                        "size_bytes": output.size_bytes,
                        "context": dict(
                            cast(dict[str, Any], artifact.payload["context"])
                        ),
                        "audition": dict(
                            cast(dict[str, Any], artifact.payload["audition"])
                        ),
                        "preview_token": token,
                        "project_sha256": project_sha256,
                        "source_receipt": source_receipt["receipt"],
                        "source_sha256": source_receipt["sha256"],
                    }
                    audio_public[output.role] = {
                        "token": audio_token,
                        "url": f"/api/restoration/audio/{audio_token}",
                        "evidence_url": (
                            f"/api/restoration/evidence/{audio_token}"
                        ),
                        "sha256": output.sha256,
                        "size_bytes": output.size_bytes,
                    }
                public = {
                    "token": token,
                    "sha256": artifact.manifest_sha256,
                    "scan_token": scan_token,
                    "candidates": selected_public,
                    "context": dict(cast(dict[str, Any], artifact.payload["context"])),
                    "audition": dict(cast(dict[str, Any], artifact.payload["audition"])),
                    "metrics": dict(cast(dict[str, Any], artifact.payload["metrics"])),
                    "proof": dict(cast(dict[str, Any], artifact.payload["proof"])),
                    "audio": audio_public,
                }
                base.update({"scan_token": scan_token, "public": public})
                self.restoration_artifacts[token] = base
                continue

            recipe_token = dependency_tokens["recipe"]
            if recipe_token not in self.restoration_artifacts:
                raise ProjectValidationError(
                    "A current restoration render has no registered recipe."
                )
            restored = next(output for output in artifact.files if output.role == "restored")
            restored_binding = dict(
                cast(
                    dict[str, Any],
                    cast(dict[str, Any], artifact.payload["files"])["restored"],
                )
            )
            restored_binding.pop("path", None)
            restored_binding["size_bytes"] = restored.size_bytes
            public = {
                "token": token,
                "sha256": artifact.manifest_sha256,
                "scan_token": scan_token,
                "recipe_token": recipe_token,
                "music_range": dict(cast(dict[str, Any], artifact.payload["music_range"])),
                "repairs": list(cast(list[dict[str, Any]], artifact.payload["repairs"])),
                "protected": list(cast(list[dict[str, Any]], artifact.payload["protected"])),
                "restored": restored_binding,
                "pcm_proof": dict(cast(dict[str, Any], artifact.payload["pcm_proof"])),
                "proof": dict(cast(dict[str, Any], artifact.payload["proof"])),
            }
            base.update(
                {
                    "scan_token": scan_token,
                    "recipe_token": recipe_token,
                    "public": public,
                }
            )
            self.restoration_artifacts[token] = base

        selection = catalog.latest_chain()
        self.latest_restoration_scan = selection.scan.artifact_id if selection.scan else None
        self.latest_restoration_recipe = selection.recipe.artifact_id if selection.recipe else None
        self.latest_restoration_preview = (
            selection.preview.artifact_id if selection.preview else None
        )
        self.latest_restoration_render = selection.render.artifact_id if selection.render else None

    def new_restoration_path(
        self,
        kind: str,
        *,
        suffix: str = "",
    ) -> Path:
        if kind not in {"scan", "preview", "recipe", "render"}:
            raise GrooveSerpentError("Unsupported restoration artifact kind.")
        if self.restoration_workspace.resolve() != self.restoration_workspace:
            raise GrooveSerpentError("The restoration workspace changed unexpectedly.")
        self.restoration_workspace.mkdir(parents=True, exist_ok=True)
        workspace = self.restoration_workspace.resolve()
        if workspace != self.restoration_workspace:
            raise GrooveSerpentError("The restoration workspace changed unexpectedly.")
        try:
            workspace.relative_to(self.project_path.parent.resolve())
        except ValueError as exc:
            raise GrooveSerpentError(
                "The restoration workspace leaves the project folder."
            ) from exc
        for _attempt in range(20):
            candidate = (workspace / f"{kind}-{uuid.uuid4().hex}{suffix}").resolve()
            try:
                candidate.relative_to(workspace)
            except ValueError as exc:
                raise GrooveSerpentError("Unsafe restoration artifact path.") from exc
            if not candidate.exists():
                return candidate
        raise GrooveSerpentError("Could not allocate a unique restoration artifact path.")

    def checked_restoration_path(
        self,
        path: Path,
        *,
        suffix: str | None = None,
        must_exist: bool = True,
    ) -> Path:
        workspace = self.restoration_workspace.resolve()
        if workspace != self.restoration_workspace:
            raise ProjectValidationError("The restoration workspace changed unexpectedly.")
        try:
            workspace.relative_to(self.project_path.parent.resolve())
        except ValueError as exc:
            raise ProjectValidationError(
                "The restoration workspace leaves the project folder."
            ) from exc
        resolved = path.resolve()
        try:
            resolved.relative_to(workspace)
        except ValueError as exc:
            raise ProjectValidationError(
                "A restoration artifact left its dedicated workspace."
            ) from exc
        if resolved == workspace:
            raise ProjectValidationError("A restoration artifact path is incomplete.")
        if suffix is not None and resolved.suffix.casefold() != suffix.casefold():
            raise ProjectValidationError("A restoration artifact has an unsafe file type.")
        if must_exist and not resolved.is_file():
            raise ProjectValidationError("A restoration artifact is missing.")
        return resolved

    def discard_restoration_path(self, path: Path) -> None:
        """Remove one unregistered server-owned artifact after a failed final lease."""

        resolved = self.checked_restoration_path(path, must_exist=False)
        if not resolved.exists():
            return
        if resolved.is_file():
            resolved.unlink()
        elif resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            raise ProjectValidationError(
                "An unregistered restoration artifact has an unsafe file type."
            )

    def begin_evidence_request(self) -> _EvidenceRequestLease:
        """Cancel prior evidence work and return the newest request generation."""

        with self._evidence_state_lock:
            if self._active_evidence_request is not None:
                self._active_evidence_request.cancelled_event.set()
            self._evidence_generation += 1
            request = _EvidenceRequestLease(
                self._evidence_generation,
                threading.Event(),
            )
            self._active_evidence_request = request
            return request

    def finish_evidence_request(self, request: _EvidenceRequestLease) -> None:
        with self._evidence_state_lock:
            if self._active_evidence_request is request:
                self._active_evidence_request = None

    def _seed_source_verification_cache(self, project: Project) -> None:
        """Trust the one-pass session capture and seed cheap review checks."""

        source = resolve_source_path(project, self.project_path).resolve()
        self.source_snapshot.assert_live_identity()
        try:
            with source.open("rb") as handle:
                before = os.fstat(handle.fileno())
                path_before = source.stat()
                observed = FileReceipt.from_stat(
                    before,
                    self.source_snapshot.live_receipt.sha256,
                )
                if not same_file_object_stats(before, path_before) or not observed.same_file_object(
                    self.source_snapshot.live_receipt
                ):
                    raise ProjectValidationError(
                        "The source changed after its session snapshot was captured."
                    )
                probe = _source_probe_signature(handle, before.st_size)
                after = os.fstat(handle.fileno())
            path_after = source.stat()
        except OSError as exc:
            raise ProjectValidationError(
                "The source could not be leased after snapshot capture."
            ) from exc
        if _stat_identity(after) != _stat_identity(before) or not same_file_object_stats(
            after, path_after
        ):
            raise ProjectValidationError(
                "The source changed after its session snapshot was captured."
            )
        key: _SourceCacheKey = (
            str(source),
            *_stat_identity(before),
            probe,
        )
        self._source_verification_cache[key] = (
            self.source_snapshot.sha256.lower(),
            uuid.uuid4().hex,
        )

    def open_verified_source(
        self,
        project: Project,
        *,
        force_full: bool = False,
        require_cached: bool = False,
    ) -> tuple[Path, BinaryIO, _SourceReceipt]:
        """Open and verify the source, returning a stable handle owned by the caller.

        The lightweight review cache is keyed by platform file identity, status-change
        time, and a bounded byte signature.  Any operation that derives or publishes
        audio passes ``force_full=True`` and therefore performs a fresh SHA-256 over
        this exact open handle.
        """

        source = resolve_source_path(project, self.project_path).resolve()
        expected_sha256 = str(project.source.sha256 or "").strip().lower()
        if not expected_sha256:
            raise ProjectValidationError(
                "This project predates source hashing. Re-analyze it before review."
            )
        handle = source.open("rb")
        try:
            before = os.fstat(handle.fileno())
            path_before = source.stat()
            if not same_file_object_stats(before, path_before):
                raise ProjectValidationError(
                    "The source path changed while it was being opened; review was refused."
                )
            if before.st_size != project.source.size_bytes:
                raise ProjectValidationError(
                    "The source audio changed after analysis; review was refused."
                )
            probe = _source_probe_signature(handle, before.st_size)
            key = (str(source), *_stat_identity(before), probe)
            cached = self._source_verification_cache.get(key)
            if cached is None and require_cached:
                raise ProjectValidationError(
                    "The source audio changed: its identity no longer matches "
                    "the start of this review session."
                )
            if cached is None or force_full:
                digest = _sha256_handle(handle).lower()
            else:
                digest = cached[0]
            after = os.fstat(handle.fileno())
            path_after = source.stat()
            if _stat_identity(after) != _stat_identity(before) or not same_file_object_stats(
                after, path_after
            ):
                raise ProjectValidationError(
                    "The source audio changed while it was being verified; review was refused."
                )
            if digest != expected_sha256:
                raise ProjectValidationError(
                    "The source audio changed after analysis; review was refused."
                )
            if cached is None or cached[0] != digest:
                cached = (digest, uuid.uuid4().hex)
                # A review session only needs recent identities for one source.
                for old_key in list(self._source_verification_cache):
                    if old_key[0] == str(source) and old_key != key:
                        self._source_verification_cache.pop(old_key, None)
                self._source_verification_cache[key] = cached
            digest, receipt_id = cached
            handle.seek(0)
            identity = _stat_identity(before)
            return (
                source,
                handle,
                {
                    "receipt": receipt_id,
                    "sha256": digest,
                    "size_bytes": before.st_size,
                    "modified_ns": before.st_mtime_ns,
                    "file_identity": {
                        "device": identity[0],
                        "inode": identity[1],
                        "change_ns": identity[4],
                        "birth_ns": identity[5],
                    },
                },
            )
        except Exception:
            handle.close()
            raise

    def verify_source(
        self,
        project: Project,
        *,
        force_full: bool = False,
        require_cached: bool = False,
    ) -> tuple[Path, _SourceReceipt]:
        """Verify source identity and close the operation handle immediately."""

        source, handle, receipt = self.open_verified_source(
            project,
            force_full=force_full,
            require_cached=require_cached,
        )
        handle.close()
        return source, receipt

    def verified_source_snapshot(
        self,
        project: Project,
        *,
        force_full: bool = True,
        evidence_lease: bool = False,
    ) -> tuple[VerifiedAudioSnapshot, _SourceReceipt]:
        """Return a borrowed session snapshot bound to the current live receipt."""

        source, receipt = self.verify_source(
            project,
            force_full=force_full,
            require_cached=evidence_lease,
        )
        expected_sha256 = str(project.source.sha256 or "").strip().lower()
        if (
            receipt["sha256"].lower() != expected_sha256
            or receipt["size_bytes"] != project.source.size_bytes
            or self.source_snapshot.sha256.lower() != expected_sha256
            or self.source_snapshot.size_bytes != project.source.size_bytes
            or self.source_snapshot.live_path != source
        ):
            raise ProjectValidationError(
                "The review session source snapshot no longer matches this project."
            )
        if evidence_lease:
            self.source_snapshot.assert_evidence_lease()
        else:
            self.source_snapshot.assert_snapshot_unchanged()
        return replace(
            self.source_snapshot,
            live_path=source,
        ), receipt

    def open_playback_snapshot(
        self,
        project: Project,
    ) -> tuple[VerifiedAudioSnapshot, BinaryIO, _SourceReceipt]:
        """Lease the immutable session snapshot without complete-file reads."""

        snapshot, receipt = self.verified_source_snapshot(
            project,
            force_full=False,
            evidence_lease=True,
        )
        try:
            handle = snapshot.path.open("rb")
        except OSError as exc:
            raise ProjectValidationError(
                "The review session source snapshot could not be opened."
            ) from exc
        try:
            self.assert_playback_snapshot_handle(snapshot, handle)
        except BaseException:
            handle.close()
            raise
        return snapshot, handle, receipt

    @staticmethod
    def assert_playback_snapshot_handle(
        snapshot: VerifiedAudioSnapshot,
        handle: BinaryIO,
    ) -> None:
        """Prove an open playback handle still names the leased snapshot object."""

        try:
            before = os.fstat(handle.fileno())
            observed = FileReceipt.from_stat(before, snapshot.sha256)
            if not observed.same_file_object(snapshot.snapshot_receipt):
                raise ProjectValidationError("The review session source snapshot handle changed.")
            snapshot.assert_evidence_lease()
            after = os.fstat(handle.fileno())
        except OSError as exc:
            raise ProjectValidationError(
                "The review session source snapshot handle could not be checked."
            ) from exc
        if _stat_identity(after) != _stat_identity(before):
            raise ProjectValidationError(
                "The review session source snapshot changed during playback."
            )


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer
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
        if self.command == "GET" and self.path in {"/", "/app.js", "/styles.css"}:
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
            "default-src 'self'; img-src 'self' data:; media-src 'self'; frame-ancestors 'none'",
        )
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if self.close_connection:
            self.send_header("Connection", "close")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        # Keep the terminal useful; only explicit errors are printed by the server.
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
        """Drain a small fixed body so Windows can deliver the error before close."""

        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            return
        remaining = int(lengths[0])
        if remaining < 0 or remaining > _MAX_REQUEST_BODY:
            return
        while remaining:
            chunk = self.rfile.read(min(64 * 1024, remaining))
            if not chunk:
                return
            remaining -= len(chunk)

    def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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

    def _read_json(self) -> dict[str, Any]:
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self.close_connection = True
            raise ProjectValidationError("Invalid Content-Length header.")
        length = int(lengths[0])
        if length <= 0 or length > _MAX_REQUEST_BODY:
            self.close_connection = True
            raise ProjectValidationError("Request body is missing or too large.")
        raw_body = self.rfile.read(length)
        if len(raw_body) != length:
            self.close_connection = True
            raise ProjectValidationError("Request body is incomplete.")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.close_connection = True
            raise ProjectValidationError("Request body is not valid JSON.") from exc
        if not isinstance(payload, dict):
            self.close_connection = True
            raise ProjectValidationError("Request body must be a JSON object.")
        return payload

    def _static(self, name: str, content_type: str) -> None:
        resource = files("groove_serpent").joinpath("web", name)
        body = resource.read_bytes()
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
        parsed_request = urlparse(self.path)
        path = parsed_request.path
        try:
            if (
                path.startswith(("/api/restoration/", "/api/endpoints/"))
                and (
                    parsed_request.query or parsed_request.fragment
                )
            ):
                raise ProjectValidationError(
                    "Review workflow endpoints do not accept query parameters."
                )
            if path == "/":
                self._static("index.html", "text/html; charset=utf-8")
            elif path == "/app.js":
                self._static("app.js", "text/javascript; charset=utf-8")
            elif path == "/styles.css":
                self._static("styles.css", "text/css; charset=utf-8")
            elif path == "/api/project":
                with self.server.operation_lock:
                    project, project_sha256 = load_project_with_sha256(self.server.project_path)
                    _source, source_receipt = self.server.verify_source(project)
                    payload = _project_payload(
                        project,
                        self.server.project_path,
                        project_sha256,
                        source_receipt,
                    )
                self._json(payload)
            elif path == "/api/ping":
                self._json({"ok": True})
            elif path == "/api/recognition/status":
                self._json(self.server.recognition_provider.readiness().to_dict())
            elif path == "/api/endpoints/status":
                self._endpoint_status()
            elif path == "/api/restoration/status":
                self._restoration_status()
            elif path == "/api/restoration/continuous/status":
                self._continuous_preview_status()
            elif path.startswith("/api/restoration/continuous/audio/"):
                self._continuous_preview_audio(path)
            elif path.startswith("/api/restoration/evidence/"):
                self._restoration_evidence(path)
            elif path.startswith("/api/restoration/audio/"):
                self._restoration_audio(path)
            elif path == "/audio":
                with self.server.operation_lock:
                    project = load_project(self.server.project_path)
                    source_snapshot, source_handle, _receipt = self.server.open_playback_snapshot(
                        project
                    )
                try:
                    self._serve_audio(
                        source_snapshot.live_path,
                        handle=source_handle,
                    )
                finally:
                    try:
                        self.server.assert_playback_snapshot_handle(
                            source_snapshot,
                            source_handle,
                        )
                    finally:
                        source_handle.close()
            elif path == "/artwork":
                project = load_project(self.server.project_path)
                self._serve_artwork(project)
            else:
                self._error(HTTPStatus.NOT_FOUND, "Not found")
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except _ProjectConflictError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except (GrooveSerpentError, OSError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unexpected server error.")

    def _serve_artwork(self, project: Project) -> None:
        stored = str(project.metadata.get("cover_art_path", "")).strip()
        if not stored:
            self._error(HTTPStatus.NOT_FOUND, "No artwork is saved for this project.")
            return
        relative = Path(stored)
        if relative.is_absolute() or ".." in relative.parts:
            raise ProjectValidationError("Saved artwork path is invalid.")
        project_root = self.server.project_path.parent.resolve()
        artwork_path = (project_root / relative).resolve()
        try:
            artwork_path.relative_to(project_root)
        except ValueError as exc:
            raise ProjectValidationError("Saved artwork path leaves the project folder.") from exc
        if not artwork_path.is_file():
            raise ProjectValidationError("Saved artwork file could not be found.")
        content_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(artwork_path.suffix.casefold())
        if content_type is None:
            raise ProjectValidationError("Saved artwork has an unsupported image type.")
        expected_sha256 = project.metadata.get("cover_art_sha256")
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256.lower())
        ):
            raise ProjectValidationError(
                "Saved artwork has no valid SHA-256 identity; artwork review was refused."
            )
        with artwork_path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > 25 * 1024 * 1024:
                raise ProjectValidationError("Saved artwork exceeds the 25 MB limit.")
            digest = _sha256_handle(handle)
            if digest != expected_sha256.lower():
                raise ProjectValidationError(
                    "Saved artwork changed after metadata review; artwork review was refused."
                )
            handle.seek(0)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            while chunk := handle.read(1024 * 1024):
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return

    def _serve_audio(self, path: Path, *, handle: BinaryIO | None = None) -> None:
        owns_handle = handle is None
        audio_handle = handle if handle is not None else path.open("rb")
        try:
            self._serve_audio_handle(path, audio_handle)
        finally:
            if owns_handle:
                audio_handle.close()

    def _serve_audio_handle(self, path: Path, handle: BinaryIO) -> None:
        total_size = os.fstat(handle.fileno()).st_size
        start = 0
        end = total_size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header:
            try:
                unit, value = range_header.split("=", 1)
                if unit.strip().lower() != "bytes" or "," in value:
                    raise ValueError
                first, last = value.split("-", 1)
                if not first and not last:
                    raise ValueError
                if first:
                    start = int(first)
                    end = int(last) if last else end
                elif last:
                    length = int(last)
                    start = max(0, total_size - length)
                if start < 0 or end < start or start >= total_size:
                    raise ValueError
                end = min(end, total_size - 1)
                status = HTTPStatus.PARTIAL_CONTENT
            except ValueError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{total_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        length = end - start + 1
        content_type = {
            ".flac": "audio/flac",
            ".m4a": "audio/mp4",
            ".wav": "audio/wav",
            ".aiff": "audio/aiff",
            ".aif": "audio/aiff",
        }.get(path.suffix.lower(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "private, no-store")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
        self.end_headers()
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return
            remaining -= len(chunk)

    def do_POST(self) -> None:  # noqa: N802
        parsed_request = urlparse(self.path)
        path = parsed_request.path
        self._pending_restoration_path: Path | None = None
        try:
            if not self._validate_post_headers():
                return
            if (
                path.startswith(("/api/restoration/", "/api/endpoints/"))
                and (
                    parsed_request.query or parsed_request.fragment
                )
            ):
                raise ProjectValidationError(
                    "Review workflow endpoints do not accept query parameters."
                )
            if path == "/api/save":
                self._save()
            elif path == "/api/export":
                self._export()
            elif path == "/api/evidence":
                self._evidence()
            elif path == "/api/metadata/search":
                self._metadata_search()
            elif path == "/api/metadata/release":
                self._metadata_release()
            elif path == "/api/metadata/apply":
                self._metadata_apply()
            elif path == "/api/topology/propose":
                self._topology_propose()
            elif path == "/api/topology/apply":
                self._topology_apply()
            elif path == "/api/endpoints/propose":
                self._endpoint_propose()
            elif path == "/api/endpoints/reject":
                self._endpoint_reject()
            elif path == "/api/endpoints/accept":
                self._endpoint_accept()
            elif path == "/api/checkpoint":
                self._checkpoint()
            elif path == "/api/recognition/identify":
                self._recognition_identify()
            elif path == "/api/restoration/scan":
                self._restoration_scan()
            elif path == "/api/restoration/preview":
                self._restoration_preview()
            elif path == "/api/restoration/recipe":
                self._restoration_recipe()
            elif path == "/api/restoration/render":
                self._restoration_render()
            elif path == "/api/restoration/continuous/context":
                self._continuous_preview_context()
            elif path == "/api/restoration/continuous/propose":
                self._continuous_preview_propose()
            elif path == "/api/restoration/continuous/preview":
                self._continuous_preview_render()
            elif path == "/api/restoration/continuous/reject":
                self._continuous_preview_reject()
            elif path == "/api/restoration/continuous/open":
                self._continuous_preview_open()
            else:
                self._error(HTTPStatus.NOT_FOUND, "Not found")
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            self._discard_pending_restoration()
            return
        except _ProjectConflictError as exc:
            self._discard_pending_restoration()
            self._error(HTTPStatus.CONFLICT, str(exc))
        except EvidenceRequestSuperseded as exc:
            self._discard_pending_restoration()
            self._error(HTTPStatus.CONFLICT, str(exc))
        except (GrooveSerpentError, OSError, ValueError) as exc:
            self._discard_pending_restoration()
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:  # defensive boundary around the local UI
            self._discard_pending_restoration()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unexpected server error.")
        finally:
            self._discard_pending_restoration()

    def _discard_pending_restoration(self) -> None:
        pending = getattr(self, "_pending_restoration_path", None)
        if pending is None:
            return
        self.server.discard_restoration_path(pending)
        self._pending_restoration_path = None

    def _restoration_request_state(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[Project, str, VerifiedAudioSnapshot, _SourceReceipt]:
        expected_state = _expected_project_state(dict(payload))
        expected_source_receipt = _expected_source_receipt(payload)
        project, project_sha256 = load_project_with_sha256(self.server.project_path)
        _assert_project_state(expected_state, project, project_sha256)
        source_snapshot, source_receipt = self.server.verified_source_snapshot(project)
        if source_receipt["receipt"] != expected_source_receipt:
            raise _ProjectConflictError(
                "The source verification receipt changed. Reload before restoration work."
            )
        return project, project_sha256, source_snapshot, source_receipt

    def _assert_restoration_inputs_unchanged(
        self,
        *,
        revision: int,
        project_sha256: str,
        source_receipt: _SourceReceipt,
    ) -> tuple[Project, _SourceReceipt]:
        current, current_sha256 = load_project_with_sha256(self.server.project_path)
        if current.revision != revision or current_sha256 != project_sha256:
            raise _ProjectConflictError("The project changed while restoration work was running.")
        _snapshot, current_receipt = self.server.verified_source_snapshot(current)
        if current_receipt.get("receipt") != source_receipt.get("receipt") or current_receipt.get(
            "sha256"
        ) != source_receipt.get("sha256"):
            raise _ProjectConflictError(
                "The source verification changed while restoration work was running."
            )
        return current, current_receipt

    def _restoration_artifact(
        self,
        token_value: Any,
        *,
        prefix: str,
        kind: str,
        project_sha256: str,
        source_receipt: _SourceReceipt,
    ) -> dict[str, Any]:
        token = _restoration_token(token_value, prefix, f"{kind.title()} token")
        artifact = self.server.restoration_artifacts.get(token)
        if artifact is None or artifact.get("kind") != kind:
            raise ProjectValidationError(
                f"The {kind} token is not registered in this server session."
            )
        if (
            artifact.get("project_sha256") != project_sha256
            or artifact.get("source_receipt") != source_receipt.get("receipt")
            or artifact.get("source_sha256") != source_receipt.get("sha256")
        ):
            raise _ProjectConflictError(
                f"The registered {kind} belongs to an older project or source state."
            )
        artifact_path = self.server.checked_restoration_path(
            Path(str(artifact.get("path", ""))), suffix=".json"
        )
        if sha256_file(artifact_path) != artifact.get("sha256"):
            raise ProjectValidationError(f"The registered {kind} changed after it was created.")
        return artifact

    @staticmethod
    def _restoration_response_state(
        project: Project,
        project_sha256: str,
        source_receipt: _SourceReceipt,
    ) -> dict[str, Any]:
        return {
            "project_revision": project.revision,
            "project_sha256": project_sha256,
            "source_receipt": dict(source_receipt),
        }

    def _restoration_status(self) -> None:
        with self.server.operation_lock:
            project, project_sha256 = load_project_with_sha256(self.server.project_path)
            _source, source_receipt = self.server.verify_source(project)

            def current_public(token: str | None) -> dict[str, Any] | None:
                if token is None:
                    return None
                entry = self.server.restoration_artifacts.get(token)
                if entry is None:
                    return None
                stale = (
                    entry.get("project_sha256") != project_sha256
                    or entry.get("source_receipt") != source_receipt["receipt"]
                    or entry.get("source_sha256") != source_receipt["sha256"]
                )
                if stale:
                    return {
                        "token": token,
                        "kind": entry.get("kind"),
                        "stale": True,
                    }
                public = dict(entry.get("public", {}))
                public["stale"] = False
                return public

            payload = {
                "ok": True,
                "persistence_scope": "verified-project-workspace",
                "restart_behavior": (
                    "Current hash-bound restoration artifacts are safely rediscovered "
                    "from the dedicated project workspace after restart. Stale or "
                    "invalid artifacts remain excluded and diagnostic-only."
                ),
                "artifact_counts": {
                    "artifacts": len(self.server.restoration_artifacts),
                    "audition_audio": len(self.server.restoration_audio),
                    "stale": self.server.restoration_catalog_diagnostics["stale"]["count"],
                    "invalid": self.server.restoration_catalog_diagnostics["invalid"]["count"],
                },
                "catalog_diagnostics": dict(self.server.restoration_catalog_diagnostics),
                "current_scan": current_public(self.server.latest_restoration_scan),
                "current_recipe": current_public(self.server.latest_restoration_recipe),
                "current_preview": current_public(self.server.latest_restoration_preview),
                "current_render": current_public(self.server.latest_restoration_render),
                **self._restoration_response_state(project, project_sha256, source_receipt),
            }
        self._json(payload)

    @staticmethod
    def _continuous_public_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
        payload = cast(dict[str, Any], entry["payload"])
        result: dict[str, Any] = {
            "artifact_kind": entry["artifact_kind"],
            "kind": entry["kind"],
            "identity_sha256": entry["identity_sha256"],
            "proposal_sha256": entry["proposal_sha256"],
            "status": entry["status"],
            "stale_reason": entry["stale_reason"],
        }
        if entry["artifact_kind"] == "proposal":
            result.update(
                {
                    "proposal_status": payload["status"],
                    "created_at": payload["created_at"],
                    "context_sha256": payload["context"]["context_sha256"],
                    "selection": payload["selection"],
                }
            )
        elif entry["artifact_kind"] == "preview":
            identity = cast(str, entry["identity_sha256"])
            result.update(
                {
                    "created_at": payload["created_at"],
                    "context_sha256": payload["context"]["context_sha256"],
                    "audio": {
                        role: {
                            **dict(payload["audio"][role]),
                            "url": (
                                "/api/restoration/continuous/audio/"
                                f"{identity}/{role}"
                            ),
                        }
                        for role in ("original", "proposed", "removed")
                    },
                }
            )
        else:
            result.update(
                {
                    "created_at": payload["created_at"],
                    "decision": payload["decision"],
                    "reason": payload["reason"],
                    "context_sha256": payload["context_sha256"],
                }
            )
        return result

    def _continuous_preview_status(self) -> None:
        with self.server.operation_lock:
            project, project_sha256 = load_project_with_sha256(self.server.project_path)
            _source, source_receipt = self.server.verify_source(project)
            catalog = discover_continuous_preview_catalog(self.server.project_path)
            payload = {
                "ok": True,
                "schema": catalog["schema"],
                "summary": catalog["summary"],
                "entries": [
                    self._continuous_public_entry(entry)
                    for entry in cast(list[dict[str, Any]], catalog["entries"])
                ],
                "invalid": catalog["invalid"],
                "authority": catalog["authority"],
                **self._restoration_response_state(project, project_sha256, source_receipt),
            }
        self._json(payload)

    def _continuous_preview_audio(self, request_path: str) -> None:
        prefix = "/api/restoration/continuous/audio/"
        suffix = request_path[len(prefix) :]
        parts = suffix.split("/")
        if len(parts) != 2 or parts[1] not in {"original", "proposed", "removed"}:
            self._error(HTTPStatus.NOT_FOUND, "Continuous-preview audio was not found.")
            return
        identity = _continuous_digest(parts[0], "Continuous-preview receipt SHA-256")
        role = parts[1]
        with self.server.operation_lock:
            project = load_project(self.server.project_path)
            self.server.verify_source(project, force_full=True)
            entry = find_current_continuous_artifact(
                self.server.project_path,
                artifact_kind="preview",
                identity_sha256=identity,
            )
            receipt = cast(dict[str, Any], entry["payload"])
            binding = cast(dict[str, Any], receipt["audio"][role])
            bundle = Path(cast(str, entry["path"]))
            audio_path = (bundle / cast(str, binding["filename"])).resolve()
            try:
                audio_path.relative_to(bundle.resolve())
            except ValueError as exc:
                raise ProjectValidationError(
                    "Continuous-preview audio left its immutable bundle."
                ) from exc
            if (
                not audio_path.is_file()
                or audio_path.stat().st_size != binding["size_bytes"]
                or sha256_file(audio_path) != binding["sha256"]
            ):
                raise ProjectValidationError(
                    "Continuous-preview audio changed after its receipt was written."
                )
            self._serve_audio(audio_path)

    def _continuous_request_state(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[Project, str, VerifiedAudioSnapshot, _SourceReceipt]:
        project, project_sha256, snapshot, source_receipt = (
            self._restoration_request_state(payload)
        )
        return project, project_sha256, snapshot, source_receipt

    def _continuous_preview_context(self) -> None:
        payload = self._read_json()
        _strict_json_object(
            payload,
            allowed={
                "action",
                "expected_revision",
                "expected_project_sha256",
                "expected_source_receipt",
                "kind",
            },
            required={
                "action",
                "expected_revision",
                "expected_project_sha256",
                "expected_source_receipt",
                "kind",
            },
            label="Continuous-preview context request",
        )
        if payload["action"] != "read-exact-continuous-preview-context":
            raise ProjectValidationError("Continuous-preview context action is unsupported.")
        if payload["kind"] not in {"hum", "rumble", "hiss", "crackle"}:
            raise ProjectValidationError("Continuous-preview kind is unsupported.")
        with self.server.operation_lock:
            project, project_sha256, _snapshot, source_receipt = (
                self._continuous_request_state(payload)
            )
            context = current_continuous_preview_context(
                self.server.project_path, cast(Any, payload["kind"])
            )
            response = {
                "ok": True,
                "context": context,
                **self._restoration_response_state(project, project_sha256, source_receipt),
            }
        self._json(response)

    def _continuous_preview_propose(self) -> None:
        payload = self._read_json()
        _strict_json_object(
            payload,
            allowed={
                "action",
                "expected_revision",
                "expected_project_sha256",
                "expected_source_receipt",
                "expected_context",
                "source_start_sample",
                "source_end_sample_exclusive",
                "owner_attested_scope_reviewed",
                "references",
            },
            required={
                "action",
                "expected_revision",
                "expected_project_sha256",
                "expected_source_receipt",
                "expected_context",
                "source_start_sample",
                "source_end_sample_exclusive",
                "owner_attested_scope_reviewed",
                "references",
            },
            label="Continuous-preview proposal request",
        )
        if payload["action"] != "propose-bounded-continuous-preview":
            raise ProjectValidationError("Continuous-preview proposal action is unsupported.")
        if payload["owner_attested_scope_reviewed"] is not True:
            raise ProjectValidationError("Continuous-preview scope requires owner review.")
        expected_context = payload["expected_context"]
        if not isinstance(expected_context, dict):
            raise ProjectValidationError("Expected continuous-preview context must be an object.")
        source = cast(dict[str, Any], expected_context.get("source", {}))
        sample_count = source.get("sample_count")
        if type(sample_count) is not int or sample_count <= 0:
            raise ProjectValidationError("Expected continuous-preview source geometry is invalid.")
        start = payload["source_start_sample"]
        end = payload["source_end_sample_exclusive"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= sample_count:
            raise ProjectValidationError("Continuous-preview source scope is invalid.")
        raw_references = payload["references"]
        if not isinstance(raw_references, list):
            raise ProjectValidationError("Continuous-preview references must be an array.")
        references = tuple(
            ReviewedNoiseReference.from_dict(item, scope_start=start, scope_end=end)
            for item in raw_references
        )
        with self.server.operation_lock:
            project, project_sha256, snapshot, source_receipt = (
                self._continuous_request_state(payload)
            )
            output, proposal = propose_continuous_preview(
                self.server.project_path,
                kind=cast(Any, expected_context.get("kind")),
                start_sample=start,
                end_sample_exclusive=end,
                references=references,
                expected_context=expected_context,
                source_snapshot=snapshot,
            )
            response = {
                "ok": True,
                "persisted": True,
                "artifact": {
                    "proposal_sha256": proposal["proposal_sha256"],
                    "status": proposal["status"],
                    "kind": proposal["kind"],
                },
                "proposal": proposal,
                "workspace_entry": output.name,
                **self._restoration_response_state(project, project_sha256, source_receipt),
            }
        self._json(response)

    def _continuous_preview_render(self) -> None:
        payload = self._read_json()
        _strict_json_object(
            payload,
            allowed={
                "action",
                "expected_revision",
                "expected_project_sha256",
                "expected_source_receipt",
                "proposal_sha256",
                "attestation",
            },
            required={
                "action",
                "expected_revision",
                "expected_project_sha256",
                "expected_source_receipt",
                "proposal_sha256",
                "attestation",
            },
            label="Continuous-preview render request",
        )
        if payload["action"] != "render-reviewed-continuous-preview":
            raise ProjectValidationError("Continuous-preview render action is unsupported.")
        proposal_sha = _continuous_digest(
            payload["proposal_sha256"], "Continuous-preview proposal SHA-256"
        )
        if not isinstance(payload["attestation"], dict):
            raise ProjectValidationError("Continuous-preview attestation must be an object.")
        with self.server.operation_lock:
            project, project_sha256, snapshot, source_receipt = (
                self._continuous_request_state(payload)
            )
            entry = find_current_continuous_artifact(
                self.server.project_path,
                artifact_kind="proposal",
                identity_sha256=proposal_sha,
            )
            proposal = cast(dict[str, Any], entry["payload"])
            attestation = validate_continuous_attestation(
                payload["attestation"], proposal
            )
            bundle, receipt = render_continuous_preview(
                self.server.project_path,
                proposal,
                attestation,
                source_snapshot=snapshot,
            )
            public = self._continuous_public_entry(
                {
                    "artifact_kind": "preview",
                    "kind": receipt["kind"],
                    "identity_sha256": receipt["receipt_sha256"],
                    "proposal_sha256": receipt["proposal_sha256"],
                    "status": "current",
                    "stale_reason": None,
                    "path": str(bundle),
                    "payload": receipt,
                }
            )
            response = {
                "ok": True,
                "persisted": True,
                "preview": public,
                "receipt": receipt,
                "workspace_entry": bundle.name,
                **self._restoration_response_state(project, project_sha256, source_receipt),
            }
        self._json(response)

    def _continuous_preview_reject(self) -> None:
        payload = self._read_json()
        _strict_json_object(
            payload,
            allowed={
                "action",
                "expected_revision",
                "expected_project_sha256",
                "expected_source_receipt",
                "proposal_sha256",
                "reason",
            },
            required={
                "action",
                "expected_revision",
                "expected_project_sha256",
                "expected_source_receipt",
                "proposal_sha256",
                "reason",
            },
            label="Continuous-preview rejection request",
        )
        if payload["action"] != "reject-continuous-preview-proposal":
            raise ProjectValidationError("Continuous-preview rejection action is unsupported.")
        proposal_sha = _continuous_digest(
            payload["proposal_sha256"], "Continuous-preview proposal SHA-256"
        )
        if not isinstance(payload["reason"], str):
            raise ProjectValidationError("Continuous-preview rejection reason must be text.")
        with self.server.operation_lock:
            project, project_sha256, _snapshot, source_receipt = (
                self._continuous_request_state(payload)
            )
            entry = find_current_continuous_artifact(
                self.server.project_path,
                artifact_kind="proposal",
                identity_sha256=proposal_sha,
            )
            output, decision = reject_continuous_proposal(
                self.server.project_path,
                cast(dict[str, Any], entry["payload"]),
                reason=payload["reason"],
            )
            response = {
                "ok": True,
                "persisted": True,
                "decision": decision,
                "workspace_entry": output.name,
                **self._restoration_response_state(project, project_sha256, source_receipt),
            }
        self._json(response)

    def _continuous_preview_open(self) -> None:
        payload = self._read_json()
        _strict_json_object(
            payload,
            allowed={
                "action",
                "expected_revision",
                "expected_project_sha256",
                "expected_source_receipt",
                "artifact_kind",
                "identity_sha256",
            },
            required={
                "action",
                "expected_revision",
                "expected_project_sha256",
                "expected_source_receipt",
                "artifact_kind",
                "identity_sha256",
            },
            label="Continuous-preview open request",
        )
        if payload["action"] != "open-current-continuous-preview-artifact":
            raise ProjectValidationError("Continuous-preview open action is unsupported.")
        artifact_kind = payload["artifact_kind"]
        if artifact_kind not in {"proposal", "preview", "decision"}:
            raise ProjectValidationError("Continuous-preview artifact kind is unsupported.")
        identity = _continuous_digest(
            payload["identity_sha256"], "Continuous-preview artifact SHA-256"
        )
        with self.server.operation_lock:
            project, project_sha256, _snapshot, source_receipt = (
                self._continuous_request_state(payload)
            )
            entry = find_current_continuous_artifact(
                self.server.project_path,
                artifact_kind=cast(Any, artifact_kind),
                identity_sha256=identity,
            )
            response = {
                "ok": True,
                "artifact": self._continuous_public_entry(entry),
                "payload": entry["payload"],
                **self._restoration_response_state(project, project_sha256, source_receipt),
            }
        self._json(response)

    def _restoration_audio(self, request_path: str) -> None:
        prefix = "/api/restoration/audio/"
        token_value = request_path[len(prefix) :]
        if not token_value or "/" in token_value or "\\" in token_value:
            self._error(HTTPStatus.NOT_FOUND, "Restoration audio token was not found.")
            return
        try:
            token = _restoration_token(token_value, "audio", "Restoration audio token")
        except ProjectValidationError:
            self._error(HTTPStatus.NOT_FOUND, "Restoration audio token was not found.")
            return
        entry = self.server.restoration_audio.get(token)
        if entry is None or entry.get("role") not in {
            "before",
            "proposed",
            "removed",
        }:
            self._error(HTTPStatus.NOT_FOUND, "Restoration audio token was not found.")
            return
        with self.server.operation_lock:
            project, project_sha256 = load_project_with_sha256(self.server.project_path)
            _source, source_receipt = self.server.verify_source(project, force_full=True)
            if (
                entry.get("project_sha256") != project_sha256
                or entry.get("source_receipt") != source_receipt["receipt"]
                or entry.get("source_sha256") != source_receipt["sha256"]
            ):
                raise _ProjectConflictError(
                    "This audition preview belongs to an older project or source state."
                )
            audio_path = self.server.checked_restoration_path(
                Path(str(entry["path"])), suffix=".flac"
            )
            if audio_path.stat().st_size != entry.get("size_bytes") or sha256_file(
                audio_path
            ) != entry.get("sha256"):
                raise ProjectValidationError(
                    "The registered restoration audio changed after preview creation."
                )
            self._serve_audio(audio_path)

    def _restoration_evidence(self, request_path: str) -> None:
        """Return aligned waveform/spectrogram evidence for one audition role."""

        prefix = "/api/restoration/evidence/"
        token_value = request_path[len(prefix) :]
        if not token_value or "/" in token_value or "\\" in token_value:
            self._error(
                HTTPStatus.NOT_FOUND,
                "Restoration evidence token was not found.",
            )
            return
        try:
            token = _restoration_token(
                token_value,
                "audio",
                "Restoration evidence token",
            )
        except ProjectValidationError:
            self._error(
                HTTPStatus.NOT_FOUND,
                "Restoration evidence token was not found.",
            )
            return
        entry = self.server.restoration_audio.get(token)
        role = entry.get("role") if entry is not None else None
        if entry is None or role not in {"before", "proposed", "removed"}:
            self._error(
                HTTPStatus.NOT_FOUND,
                "Restoration evidence token was not found.",
            )
            return

        with self.server.operation_lock:
            project, project_sha256 = load_project_with_sha256(
                self.server.project_path
            )
            _source, source_receipt = self.server.verify_source(
                project,
                require_cached=True,
            )
            if (
                entry.get("project_sha256") != project_sha256
                or entry.get("source_receipt") != source_receipt["receipt"]
                or entry.get("source_sha256") != source_receipt["sha256"]
            ):
                raise _ProjectConflictError(
                    "This visual audition proof belongs to an older project or "
                    "source state."
                )
            audio_path = self.server.checked_restoration_path(
                Path(str(entry["path"])),
                suffix=".flac",
            )
            stat = audio_path.stat()
            if stat.st_size != entry.get("size_bytes") or sha256_file(
                audio_path
            ) != entry.get("sha256"):
                raise ProjectValidationError(
                    "The registered restoration audio changed after preview creation."
                )

            context = entry.get("context")
            audition = entry.get("audition")
            required_context = {
                "start_frame",
                "end_frame_exclusive",
                "repair_start_in_preview",
                "repair_end_in_preview_exclusive",
                "repair_windows",
            }
            if (
                type(context) is not dict
                or not required_context.issubset(context)
                or type(audition) is not dict
            ):
                raise ProjectValidationError(
                    "The restoration preview has no aligned visual context."
                )
            integers: dict[str, int] = {}
            for name in required_context - {"repair_windows"}:
                value = context[name]
                if type(value) is not int:
                    raise ProjectValidationError(
                        "The restoration preview visual context is invalid."
                    )
                integers[name] = value
            source_start = integers["start_frame"]
            source_end = integers["end_frame_exclusive"]
            repair_start = integers["repair_start_in_preview"]
            repair_end = integers["repair_end_in_preview_exclusive"]
            sample_count = source_end - source_start
            if (
                source_start < 0
                or sample_count <= 0
                or not 0 <= repair_start < repair_end <= sample_count
            ):
                raise ProjectValidationError(
                    "The restoration preview visual context is invalid."
                )

            maximum_frames = max(
                1,
                round(project.source.sample_rate * MAX_EVIDENCE_SECONDS),
            )
            focus_sample = repair_start + (repair_end - repair_start) // 2
            evidence_start = max(0, focus_sample - maximum_frames // 2)
            evidence_end = min(sample_count, evidence_start + maximum_frames)
            evidence_start = max(0, evidence_end - maximum_frames)
            preview_source = AudioSource(
                path=audio_path.name,
                filename=f"{role}.flac",
                size_bytes=stat.st_size,
                modified_ns=stat.st_mtime_ns,
                duration_seconds=sample_count / project.source.sample_rate,
                sample_rate=project.source.sample_rate,
                channels=project.source.channels,
                codec_name="flac",
                bits_per_raw_sample=project.source.bits_per_raw_sample,
                sample_format=project.source.sample_format,
                sample_count=sample_count,
                sha256=cast(str, entry["sha256"]),
            )
            with verified_audio_snapshot(
                audio_path,
                expected_sha256=preview_source.sha256,
                expected_size_bytes=preview_source.size_bytes,
                workspace=self.server.source_snapshot_workspace,
                label=f"Restoration {role} audition audio",
            ) as audition_snapshot:
                evidence = analyze_evidence_window(
                    audio_path,
                    preview_source,
                    start_sample=evidence_start,
                    end_sample=evidence_end,
                    focus_sample=focus_sample,
                    waveform_points=800,
                    spectrogram_time_bins=180,
                    spectrogram_frequency_bins=72,
                    source_snapshot=audition_snapshot,
                )
            gain_key = {
                "before": "before_linear_gain",
                "proposed": "proposed_linear_gain",
                "removed": "removed_linear_gain",
            }[cast(str, role)]
            payload = {
                "ok": True,
                "preview_token": entry["preview_token"],
                "audio_token": token,
                "role": role,
                "audio_sha256": entry["sha256"],
                "evidence": evidence,
                "alignment": {
                    "coordinate_space": "source-sample",
                    "source_start_sample": source_start + evidence_start,
                    "source_end_sample_exclusive": source_start + evidence_end,
                    "focus_source_sample": source_start + focus_sample,
                    "repair_start_source_sample": source_start + repair_start,
                    "repair_end_source_sample_exclusive": source_start
                    + repair_end,
                    "matched_audio_geometry": True,
                    "declared_linear_gain": audition.get(gain_key),
                    "matched_original_level": audition.get(
                        "matched_original_level"
                    ),
                },
                **self._restoration_response_state(
                    project,
                    project_sha256,
                    source_receipt,
                ),
            }
        self._json(payload)

    def _restoration_scan(self) -> None:
        payload = self._read_json()
        base = {
            "expected_revision",
            "expected_project_sha256",
            "expected_source_receipt",
        }
        _strict_json_object(
            payload,
            allowed=base | {"start_seconds", "end_seconds", "max_candidates"},
            required=base,
            label="Restoration scan request",
        )
        with self.server.operation_lock:
            project, project_sha256, source_snapshot, source_receipt = (
                self._restoration_request_state(payload)
            )
            duration = project.source.duration_seconds
            start_seconds = None
            end_seconds = None
            if "start_seconds" in payload:
                start_seconds = _finite_json_number(
                    payload["start_seconds"],
                    label="Scan start",
                    minimum=0.0,
                    maximum=duration,
                )
            if "end_seconds" in payload:
                end_seconds = _finite_json_number(
                    payload["end_seconds"],
                    label="Scan end",
                    minimum=0.0,
                    maximum=duration,
                )
            effective_start = 0.0 if start_seconds is None else start_seconds
            effective_end = duration if end_seconds is None else end_seconds
            if effective_end <= effective_start:
                raise ProjectValidationError("Scan end must be after scan start.")
            if round((effective_end - effective_start) * project.source.sample_rate) < 256:
                raise ProjectValidationError(
                    "The restoration scan must contain at least 256 frames."
                )
            max_candidates = payload.get("max_candidates", 500)
            if type(max_candidates) is not int or not 1 <= max_candidates <= 10_000:
                raise ProjectValidationError(
                    "Maximum candidates must be a JSON integer from 1 to 10000."
                )
            scan_path = self.server.new_restoration_path("scan", suffix=".json")
            self._pending_restoration_path = scan_path
            scan_project_clicks(
                self.server.project_path,
                scan_path,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                max_candidates=max_candidates,
                source_snapshot=source_snapshot,
            )
            scan_path = self.server.checked_restoration_path(scan_path, suffix=".json")
            scan_payload = _read_restoration_json(scan_path, SCAN_SCHEMA)
            try:
                self._assert_restoration_inputs_unchanged(
                    revision=project.revision,
                    project_sha256=project_sha256,
                    source_receipt=source_receipt,
                )
            except BaseException:
                self.server.discard_restoration_path(scan_path)
                raise
            digest = sha256_file(scan_path)
            token = self.server._stable_content_token(
                "scan", digest, self.server.restoration_artifacts
            )
            public = _compact_restoration_scan(scan_payload, token=token, digest=digest)
            self.server.restoration_artifacts[token] = {
                "kind": "scan",
                "path": scan_path,
                "sha256": digest,
                "project_revision": project.revision,
                "project_sha256": project_sha256,
                "source_receipt": source_receipt["receipt"],
                "source_sha256": source_receipt["sha256"],
                "payload": scan_payload,
                "public": public,
            }
            self._pending_restoration_path = None
            self.server.latest_restoration_scan = token
            self.server.latest_restoration_recipe = None
            self.server.latest_restoration_preview = None
            self.server.latest_restoration_render = None
            response = {
                "ok": True,
                "scan": public,
                **self._restoration_response_state(project, project_sha256, source_receipt),
            }
        self._json(response)

    def _restoration_preview(self) -> None:
        payload = self._read_json()
        base = {
            "expected_revision",
            "expected_project_sha256",
            "expected_source_receipt",
            "scan_token",
            "candidate_ids",
        }
        _strict_json_object(
            payload,
            allowed=base | {"context_seconds"},
            required=base,
            label="Restoration preview request",
        )
        with self.server.operation_lock:
            project, project_sha256, source_snapshot, source_receipt = (
                self._restoration_request_state(payload)
            )
            scan = self._restoration_artifact(
                payload["scan_token"],
                prefix="scan",
                kind="scan",
                project_sha256=project_sha256,
                source_receipt=source_receipt,
            )
            candidate_ids = payload.get("candidate_ids")
            if (
                type(candidate_ids) is not list
                or not 1 <= len(candidate_ids) <= MAX_PREVIEW_CANDIDATES
                or any(
                    not isinstance(value, str) or not value.startswith("clk-") or len(value) > 160
                    for value in candidate_ids
                )
                or len(set(candidate_ids)) != len(candidate_ids)
            ):
                raise ProjectValidationError(
                    f"Candidate IDs must contain 1 to {MAX_PREVIEW_CANDIDATES} unique click IDs."
                )
            compact_candidates = scan["public"]["candidates"]
            candidates_by_id = {item["id"]: item for item in compact_candidates}
            if any(candidate_id not in candidates_by_id for candidate_id in candidate_ids):
                raise ProjectValidationError(
                    "A preview candidate ID is not in the registered scan."
                )
            if any(
                candidates_by_id[candidate_id]["repairable"] is not True
                for candidate_id in candidate_ids
            ):
                raise ProjectValidationError(
                    "Only repairable candidates can have a proposed preview."
                )
            context_seconds = _finite_json_number(
                payload.get("context_seconds", 2.0),
                label="Preview context",
                minimum=0.1,
                maximum=30.0,
            )
            bundle = self.server.new_restoration_path("preview")
            self._pending_restoration_path = bundle
            create_click_preview(
                self.server.project_path,
                Path(scan["path"]),
                list(candidate_ids),
                bundle,
                context_seconds=context_seconds,
                source_snapshot=source_snapshot,
            )
            bundle = self.server.checked_restoration_path(bundle, must_exist=False)
            if not bundle.is_dir():
                raise ProjectValidationError("The preview bundle is missing.")
            manifest_path = self.server.checked_restoration_path(
                bundle / "preview.json", suffix=".json"
            )
            manifest = _read_restoration_json(manifest_path, PREVIEW_SCHEMA)
            try:
                self._assert_restoration_inputs_unchanged(
                    revision=project.revision,
                    project_sha256=project_sha256,
                    source_receipt=source_receipt,
                )
            except BaseException:
                self.server.discard_restoration_path(bundle)
                raise
            files_payload = manifest.get("files")
            if type(files_payload) is not dict or set(files_payload) != {
                "before",
                "proposed",
                "removed",
            }:
                raise ProjectValidationError(
                    "The preview manifest does not contain the three audition files."
                )
            digest = sha256_file(manifest_path)
            preview_token = self.server._stable_content_token(
                "preview", digest, self.server.restoration_artifacts
            )
            audio_public: dict[str, dict[str, Any]] = {}
            pending_audio: dict[str, dict[str, Any]] = {}
            for role in ("before", "proposed", "removed"):
                binding = files_payload[role]
                if type(binding) is not dict or set(binding) != {"path", "sha256"}:
                    raise ProjectValidationError("A preview audio binding is invalid.")
                relative = binding.get("path")
                expected_sha256 = binding.get("sha256")
                if (
                    not isinstance(relative, str)
                    or not relative
                    or Path(relative).is_absolute()
                    or ".." in Path(relative).parts
                    or not isinstance(expected_sha256, str)
                    or len(expected_sha256) != 64
                ):
                    raise ProjectValidationError("A preview audio binding is unsafe.")
                audio_path = self.server.checked_restoration_path(bundle / relative, suffix=".flac")
                try:
                    audio_path.relative_to(bundle.resolve())
                except ValueError as exc:
                    raise ProjectValidationError("A preview audio file left its bundle.") from exc
                observed_sha256 = sha256_file(audio_path)
                if observed_sha256 != expected_sha256:
                    raise ProjectValidationError(
                        "A preview audio file does not match its manifest."
                    )
                audio_token = self.server._stable_audio_token(
                    preview_token,
                    role,
                    observed_sha256,
                    {**self.server.restoration_audio, **pending_audio},
                )
                entry = {
                    "role": role,
                    "path": audio_path,
                    "sha256": observed_sha256,
                    "size_bytes": audio_path.stat().st_size,
                    "context": dict(manifest.get("context", {})),
                    "audition": dict(manifest.get("audition", {})),
                    "preview_token": preview_token,
                    "project_sha256": project_sha256,
                    "source_receipt": source_receipt["receipt"],
                    "source_sha256": source_receipt["sha256"],
                }
                pending_audio[audio_token] = entry
                audio_public[role] = {
                    "token": audio_token,
                    "url": f"/api/restoration/audio/{audio_token}",
                    "evidence_url": (
                        f"/api/restoration/evidence/{audio_token}"
                    ),
                    "sha256": observed_sha256,
                    "size_bytes": entry["size_bytes"],
                }
            public = {
                "token": preview_token,
                "sha256": digest,
                "scan_token": payload["scan_token"],
                "candidates": [candidates_by_id[candidate_id] for candidate_id in candidate_ids],
                "context": manifest.get("context", {}),
                "audition": manifest.get("audition", {}),
                "metrics": manifest.get("metrics", {}),
                "proof": manifest.get("proof", {}),
                "audio": audio_public,
            }
            self.server.restoration_audio.update(pending_audio)
            self.server.restoration_artifacts[preview_token] = {
                "kind": "preview",
                "path": manifest_path,
                "sha256": digest,
                "project_revision": project.revision,
                "project_sha256": project_sha256,
                "source_receipt": source_receipt["receipt"],
                "source_sha256": source_receipt["sha256"],
                "scan_token": payload["scan_token"],
                "payload": manifest,
                "public": public,
            }
            self._pending_restoration_path = None
            self.server.latest_restoration_preview = preview_token
            response = {
                "ok": True,
                "preview": public,
                **self._restoration_response_state(project, project_sha256, source_receipt),
            }
        self._json(response)

    def _restoration_recipe(self) -> None:
        payload = self._read_json()
        required = {
            "expected_revision",
            "expected_project_sha256",
            "expected_source_receipt",
            "scan_token",
            "decisions",
        }
        _strict_json_object(
            payload,
            allowed=required,
            required=required,
            label="Restoration recipe request",
        )
        with self.server.operation_lock:
            project, project_sha256, source_snapshot, source_receipt = (
                self._restoration_request_state(payload)
            )
            scan = self._restoration_artifact(
                payload["scan_token"],
                prefix="scan",
                kind="scan",
                project_sha256=project_sha256,
                source_receipt=source_receipt,
            )
            decisions_raw = payload.get("decisions")
            candidates = {item["id"]: item for item in scan["public"]["candidates"]}
            if type(decisions_raw) is not list or len(decisions_raw) > 10_000:
                raise ProjectValidationError("Recipe decisions must be a bounded JSON array.")
            decisions: list[dict[str, Any]] = []
            seen: set[str] = set()
            for raw in decisions_raw:
                if type(raw) is not dict:
                    raise ProjectValidationError(
                        "Each recipe decision must be a strict JSON object."
                    )
                decision_value = raw.get("decision")
                expected_keys = {"candidate_id", "decision"}
                if decision_value == "protected":
                    expected_keys.add("classification")
                if set(raw) != expected_keys:
                    raise ProjectValidationError(
                        "Each recipe decision has unsupported or missing fields."
                    )
                candidate_id = raw.get("candidate_id")
                if not isinstance(candidate_id, str) or candidate_id not in candidates:
                    raise ProjectValidationError("A recipe decision has an unknown candidate ID.")
                if candidate_id in seen:
                    raise ProjectValidationError("A candidate may be decided only once.")
                seen.add(candidate_id)
                if (
                    not isinstance(decision_value, str)
                    or decision_value not in _RESTORATION_DECISIONS
                ):
                    raise ProjectValidationError(
                        "A recipe decision must be approved, rejected, or protected."
                    )
                if (
                    decision_value == "approved"
                    and candidates[candidate_id]["repairable"] is not True
                ):
                    raise ProjectValidationError("A non-repairable candidate cannot be approved.")
                if decision_value == "protected":
                    classification = raw.get("classification")
                    if (
                        not isinstance(classification, str)
                        or classification not in _RESTORATION_PROTECTED_CLASSIFICATIONS
                    ):
                        raise ProjectValidationError(
                            "A protected candidate requires a needle/handling classification."
                        )
                decisions.append(dict(raw))
            if seen != set(candidates):
                raise ProjectValidationError(
                    "The recipe must decide every retained scan candidate exactly once."
                )
            recipe_path = self.server.new_restoration_path("recipe", suffix=".json")
            self._pending_restoration_path = recipe_path
            create_restoration_recipe(
                self.server.project_path,
                Path(scan["path"]),
                decisions,
                recipe_path,
                source_snapshot=source_snapshot,
            )
            recipe_path = self.server.checked_restoration_path(recipe_path, suffix=".json")
            recipe_payload = _read_restoration_json(recipe_path, RECIPE_SCHEMA)
            try:
                self._assert_restoration_inputs_unchanged(
                    revision=project.revision,
                    project_sha256=project_sha256,
                    source_receipt=source_receipt,
                )
            except BaseException:
                self.server.discard_restoration_path(recipe_path)
                raise
            digest = sha256_file(recipe_path)
            recipe_token = self.server._stable_content_token(
                "recipe", digest, self.server.restoration_artifacts
            )
            public = {
                "token": recipe_token,
                "sha256": digest,
                "scan_token": payload["scan_token"],
                "created_at": str(recipe_payload.get("created_at", ""))[:200],
                "summary": recipe_payload.get("summary", {}),
                "decisions": decisions,
            }
            if isinstance(recipe_payload.get("coverage"), dict):
                public["coverage"] = dict(recipe_payload["coverage"])
            self.server.restoration_artifacts[recipe_token] = {
                "kind": "recipe",
                "path": recipe_path,
                "sha256": digest,
                "project_revision": project.revision,
                "project_sha256": project_sha256,
                "source_receipt": source_receipt["receipt"],
                "source_sha256": source_receipt["sha256"],
                "scan_token": payload["scan_token"],
                "payload": recipe_payload,
                "public": public,
            }
            self._pending_restoration_path = None
            self.server.latest_restoration_recipe = recipe_token
            self.server.latest_restoration_render = None
            response = {
                "ok": True,
                "recipe": public,
                **self._restoration_response_state(project, project_sha256, source_receipt),
            }
        self._json(response)

    def _restoration_render(self) -> None:
        payload = self._read_json()
        required = {
            "expected_revision",
            "expected_project_sha256",
            "expected_source_receipt",
            "scan_token",
            "recipe_token",
        }
        _strict_json_object(
            payload,
            allowed=required,
            required=required,
            label="Restoration render request",
        )
        with self.server.operation_lock:
            project, project_sha256, source_snapshot, source_receipt = (
                self._restoration_request_state(payload)
            )
            scan = self._restoration_artifact(
                payload["scan_token"],
                prefix="scan",
                kind="scan",
                project_sha256=project_sha256,
                source_receipt=source_receipt,
            )
            recipe = self._restoration_artifact(
                payload["recipe_token"],
                prefix="recipe",
                kind="recipe",
                project_sha256=project_sha256,
                source_receipt=source_receipt,
            )
            if recipe.get("scan_token") != payload["scan_token"]:
                raise ProjectValidationError("The recipe token belongs to a different scan.")
            bundle = self.server.new_restoration_path("render")
            self._pending_restoration_path = bundle
            render_restored_side(
                self.server.project_path,
                Path(scan["path"]),
                Path(recipe["path"]),
                bundle,
                source_snapshot=source_snapshot,
            )
            bundle = self.server.checked_restoration_path(bundle, must_exist=False)
            if not bundle.is_dir():
                raise ProjectValidationError("The restoration render bundle is missing.")
            manifest_path = self.server.checked_restoration_path(
                bundle / "render.json", suffix=".json"
            )
            manifest = _read_restoration_json(manifest_path, RENDER_SCHEMA)
            try:
                self._assert_restoration_inputs_unchanged(
                    revision=project.revision,
                    project_sha256=project_sha256,
                    source_receipt=source_receipt,
                )
            except BaseException:
                self.server.discard_restoration_path(bundle)
                raise
            files_payload = manifest.get("files")
            restored_binding = (
                files_payload.get("restored") if type(files_payload) is dict else None
            )
            if type(restored_binding) is not dict:
                raise ProjectValidationError(
                    "The restoration receipt has no restored FLAC binding."
                )
            relative = restored_binding.get("path")
            expected_sha256 = restored_binding.get("sha256")
            if (
                not isinstance(relative, str)
                or not relative
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or not isinstance(expected_sha256, str)
                or len(expected_sha256) != 64
            ):
                raise ProjectValidationError("The restored FLAC binding is unsafe.")
            restored_path = self.server.checked_restoration_path(bundle / relative, suffix=".flac")
            try:
                restored_path.relative_to(bundle.resolve())
            except ValueError as exc:
                raise ProjectValidationError("The restored FLAC left its bundle.") from exc
            if sha256_file(restored_path) != expected_sha256:
                raise ProjectValidationError("The restored FLAC does not match its render receipt.")
            digest = sha256_file(manifest_path)
            render_token = self.server._stable_content_token(
                "render", digest, self.server.restoration_artifacts
            )
            safe_restored = {key: value for key, value in restored_binding.items() if key != "path"}
            safe_restored["size_bytes"] = restored_path.stat().st_size
            public = {
                "token": render_token,
                "sha256": digest,
                "scan_token": payload["scan_token"],
                "recipe_token": payload["recipe_token"],
                "music_range": manifest.get("music_range", {}),
                "repairs": manifest.get("repairs", []),
                "protected": manifest.get("protected", []),
                "restored": safe_restored,
                "pcm_proof": manifest.get("pcm_proof", {}),
                "proof": manifest.get("proof", {}),
            }
            self.server.restoration_artifacts[render_token] = {
                "kind": "render",
                "path": manifest_path,
                "sha256": digest,
                "project_revision": project.revision,
                "project_sha256": project_sha256,
                "source_receipt": source_receipt["receipt"],
                "source_sha256": source_receipt["sha256"],
                "scan_token": payload["scan_token"],
                "recipe_token": payload["recipe_token"],
                "payload": manifest,
                "public": public,
            }
            self._pending_restoration_path = None
            self.server.latest_restoration_render = render_token
            response = {
                "ok": True,
                "render": public,
                **self._restoration_response_state(project, project_sha256, source_receipt),
            }
        self._json(response)

    @staticmethod
    def _endpoint_document_matches_project(
        proposal: Mapping[str, Any],
        project: Project,
        project_sha256: str,
    ) -> bool:
        """Return whether one validated proposal names this exact editable state."""

        expected_project = {
            "sha256": project_sha256,
            "revision": project.revision,
            "state_sha256": project.state_sha256,
        }
        expected_source = {
            "sha256": project.source.sha256,
            "size_bytes": project.source.size_bytes,
            "sample_rate": project.source.sample_rate,
            "channels": project.source.channels,
            "bits_per_raw_sample": project.source.bits_per_raw_sample,
            "sample_count": project.source.sample_count,
            "codec_name": project.source.codec_name,
        }
        return (
            proposal.get("project") == expected_project
            and proposal.get("source") == expected_source
        )

    def _endpoint_request_state(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[Project, str, _SourceReceipt]:
        expected_state = _expected_project_state(dict(payload))
        expected_source_receipt = _expected_source_receipt(payload)
        project, project_sha256 = load_project_with_sha256(
            self.server.project_path
        )
        _assert_project_state(expected_state, project, project_sha256)
        _snapshot, source_receipt = self.server.verified_source_snapshot(project)
        if source_receipt["receipt"] != expected_source_receipt:
            raise _ProjectConflictError(
                "The source verification receipt changed. Reload before endpoint review."
            )
        return project, project_sha256, source_receipt

    def _current_endpoint_proposal(
        self,
        project: Project,
        project_sha256: str,
        source_receipt: _SourceReceipt,
        proposal_sha256: Any,
    ) -> dict[str, Any]:
        if (
            not isinstance(proposal_sha256, str)
            or len(proposal_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in proposal_sha256
            )
        ):
            raise ProjectValidationError(
                "Endpoint proposal SHA-256 must be one lowercase digest."
            )
        cached = self.server.endpoint_proposal
        if cached is None:
            raise ProjectValidationError(
                "There is no pending endpoint proposal in this review session."
            )
        proposal = validate_endpoint_proposal_document(cached)
        if proposal["proposal_sha256"] != proposal_sha256:
            raise _ProjectConflictError(
                "The pending endpoint proposal changed. Review the current evidence again."
            )
        if (
            self.server.endpoint_proposal_source_receipt
            != source_receipt["receipt"]
            or not self._endpoint_document_matches_project(
                proposal,
                project,
                project_sha256,
            )
        ):
            self.server.endpoint_proposal = None
            self.server.endpoint_proposal_source_receipt = None
            raise _ProjectConflictError(
                "The project or source changed after endpoint analysis. "
                "Create and review a new proposal."
            )
        return proposal

    def _endpoint_status(self) -> None:
        with self.server.operation_lock:
            project, project_sha256 = load_project_with_sha256(
                self.server.project_path
            )
            _source, source_receipt = self.server.verify_source(project)
            proposal = self.server.endpoint_proposal
            state = "empty"
            if proposal is not None:
                proposal = validate_endpoint_proposal_document(proposal)
                if (
                    self.server.endpoint_proposal_source_receipt
                    == source_receipt["receipt"]
                    and self._endpoint_document_matches_project(
                        proposal,
                        project,
                        project_sha256,
                    )
                ):
                    state = "pending-review"
                else:
                    proposal = None
                    state = "stale-cleared"
                    self.server.endpoint_proposal = None
                    self.server.endpoint_proposal_source_receipt = None
        self._json(
            {
                "ok": True,
                "state": state,
                "proposal": proposal,
                "project_revision": project.revision,
                "project_sha256": project_sha256,
                "source_receipt": source_receipt,
                "authority": "review-only-never-inferred",
            }
        )

    def _endpoint_propose(self) -> None:
        payload = self._read_json()
        required = {
            "expected_revision",
            "expected_project_sha256",
            "expected_source_receipt",
            "scope_label",
        }
        _strict_json_object(
            payload,
            allowed=required,
            required=required,
            label="Endpoint proposal request",
        )
        label = payload["scope_label"]
        if (
            not isinstance(label, str)
            or label != label.strip()
            or not label
            or len(label) > 64
            or any(ord(character) < 32 for character in label)
        ):
            raise ProjectValidationError(
                "Endpoint scope label must be bounded, trimmed, printable text."
            )
        with self.server.operation_lock:
            project, project_sha256, source_receipt = (
                self._endpoint_request_state(payload)
            )
            sample_count = project.source.sample_count
            if type(sample_count) is not int or sample_count <= 0:
                raise ProjectValidationError(
                    "Endpoint review requires an exact positive source sample count."
                )
            proposal = analyze_endpoint_proposals(
                self.server.project_path,
                (EndpointScope(label, 0, sample_count),),
                snapshot_workspace=self.server.source_snapshot_workspace,
            )
            current, current_sha256 = load_project_with_sha256(
                self.server.project_path
            )
            _assert_project_state(
                (project.revision, project_sha256),
                current,
                current_sha256,
            )
            _snapshot, current_source_receipt = (
                self.server.verified_source_snapshot(current)
            )
            if current_source_receipt["receipt"] != source_receipt["receipt"]:
                raise _ProjectConflictError(
                    "The source changed while endpoint evidence was being analyzed."
                )
            proposal = validate_endpoint_proposal_document(proposal)
            if not self._endpoint_document_matches_project(
                proposal,
                current,
                current_sha256,
            ):
                raise _ProjectConflictError(
                    "Endpoint analysis returned evidence for a different project state."
                )
            self.server.endpoint_proposal = proposal
            self.server.endpoint_proposal_source_receipt = (
                current_source_receipt["receipt"]
            )
        self._json(
            {
                "ok": True,
                "state": "pending-review",
                "proposal": proposal,
                "project_revision": current.revision,
                "project_sha256": current_sha256,
                "source_receipt": current_source_receipt,
                "authority": "review-only-never-inferred",
            }
        )

    def _endpoint_reject(self) -> None:
        payload = self._read_json()
        required = {
            "expected_revision",
            "expected_project_sha256",
            "expected_source_receipt",
            "proposal_sha256",
            "decision",
        }
        _strict_json_object(
            payload,
            allowed=required | {"reason"},
            required=required,
            label="Endpoint rejection request",
        )
        if payload["decision"] != "reject":
            raise ProjectValidationError(
                "Endpoint rejection requires the literal 'reject' decision."
            )
        reason = payload.get("reason", "")
        if (
            not isinstance(reason, str)
            or reason != reason.strip()
            or len(reason) > 500
            or any(ord(character) < 32 for character in reason)
        ):
            raise ProjectValidationError(
                "Endpoint rejection reason must be bounded, trimmed, printable text."
            )
        with self.server.operation_lock:
            project, project_sha256, source_receipt = (
                self._endpoint_request_state(payload)
            )
            proposal = self._current_endpoint_proposal(
                project,
                project_sha256,
                source_receipt,
                payload["proposal_sha256"],
            )
            self.server.endpoint_proposal = None
            self.server.endpoint_proposal_source_receipt = None
        self._json(
            {
                "ok": True,
                "review_decision": "rejected",
                "proposal_sha256": proposal["proposal_sha256"],
                "reason": reason,
                "project_mutated": False,
            }
        )

    def _endpoint_accept(self) -> None:
        payload = self._read_json()
        required = {
            "expected_revision",
            "expected_project_sha256",
            "expected_source_receipt",
            "proposal_sha256",
            "decision",
            "intent",
            "reviewed_start",
            "reviewed_end",
        }
        _strict_json_object(
            payload,
            allowed=required,
            required=required,
            label="Endpoint acceptance request",
        )
        if payload["decision"] != "accept":
            raise ProjectValidationError(
                "Endpoint acceptance requires the literal 'accept' decision."
            )
        if payload["intent"] != _ENDPOINT_REVIEW_INTENT:
            raise ProjectValidationError(
                "Endpoint acceptance requires the explicit no-runout review intent."
            )
        if payload["reviewed_start"] is not True or payload["reviewed_end"] is not True:
            raise ProjectValidationError(
                "Audition and inspect both proposed endpoints before acceptance."
            )
        with self.server.operation_lock:
            project, project_sha256, source_receipt = (
                self._endpoint_request_state(payload)
            )
            proposal = self._current_endpoint_proposal(
                project,
                project_sha256,
                source_receipt,
                payload["proposal_sha256"],
            )
            scopes = proposal["scopes"]
            sample_count = project.source.sample_count
            if (
                len(scopes) != 1
                or scopes[0]["scope_start_sample"] != 0
                or scopes[0]["scope_end_sample_exclusive"] != sample_count
            ):
                raise ProjectValidationError(
                    "The side review can accept only one full-source endpoint scope."
                )
            scope = scopes[0]
            if scope["status"] != "proposed" or scope["requires_review"] is not True:
                raise ProjectValidationError(
                    "This endpoint analysis abstained; there is no suggestion to accept."
                )
            start_sample = scope["proposed_music_start_sample"]
            end_sample = scope["proposed_music_end_sample_exclusive"]
            if type(start_sample) is not int or type(end_sample) is not int:
                raise ProjectValidationError(
                    "The endpoint proposal has no exact samples to accept."
                )
            first = project.tracks[0]
            last = project.tracks[-1]
            if start_sample >= first.end_sample or end_sample <= last.start_sample:
                raise ProjectValidationError(
                    "The proposed endpoints would erase or invert a reviewed track."
                )
            if (
                start_sample == first.start_sample
                and end_sample == last.end_sample
            ):
                raise ProjectValidationError(
                    "The reviewed project already uses these exact endpoints."
                )
            before_state = project.capture_state()
            sample_rate = project.source.sample_rate
            if len(project.tracks) == 1:
                project.tracks[0] = replace(
                    first,
                    start_sample=start_sample,
                    end_sample=end_sample,
                    start_seconds=start_sample / sample_rate,
                    end_seconds=end_sample / sample_rate,
                )
            else:
                project.tracks[0] = replace(
                    first,
                    start_sample=start_sample,
                    start_seconds=start_sample / sample_rate,
                )
                project.tracks[-1] = replace(
                    last,
                    end_sample=end_sample,
                    end_seconds=end_sample / sample_rate,
                )
            history = project.append_history(
                action="move_marker",
                summary=(
                    "Accepted reviewed music endpoints; removed lead-in and "
                    "runout while preserving wanted music"
                ),
                before=before_state,
                after=project.capture_state(),
            )
            save_project(
                project,
                self.server.project_path,
                expected_existing_sha256=project_sha256,
            )
            project, project_sha256 = load_project_with_sha256(
                self.server.project_path
            )
            _source, source_receipt = self.server.verify_source(
                project,
                force_full=True,
            )
            response_project = _project_payload(
                project,
                self.server.project_path,
                project_sha256,
                source_receipt,
            )
            self.server.endpoint_proposal = None
            self.server.endpoint_proposal_source_receipt = None
        self._json(
            {
                "ok": True,
                "review_decision": "accepted",
                "proposal_sha256": proposal["proposal_sha256"],
                "accepted_start_sample": start_sample,
                "accepted_end_sample_exclusive": end_sample,
                "history_sequence": history.sequence,
                "project": response_project,
            }
        )

    def _save(self) -> None:
        payload = self._read_json()
        required = {
            "expected_revision",
            "expected_project_sha256",
            "metadata",
            "tracks",
        }
        _strict_json_object(
            payload,
            allowed=required,
            required=required,
            label="Project save request",
        )
        expected_state = _expected_project_state(payload)
        with self.server.operation_lock:
            project, project_sha256 = load_project_with_sha256(self.server.project_path)
            _assert_project_state(expected_state, project, project_sha256)
            self.server.verify_source(project, force_full=True)
            before_state = project.capture_state()
            track_payloads = payload.get("tracks")
            if not isinstance(track_payloads, list) or not track_payloads:
                raise ProjectValidationError("A saved project must contain at least one track.")

            sample_rate = project.source.sample_rate
            maximum_sample = (
                project.source.sample_count
                if project.source.sample_count is not None
                else int(round(project.source.duration_seconds * sample_rate)) + 2
            )
            updated_tracks: list[Track] = []
            editable_text = (
                "title",
                "artist",
                "album",
                "album_artist",
                "year",
                "genre",
                "side",
            )
            for index, item in enumerate(track_payloads, start=1):
                if not isinstance(item, dict):
                    raise ProjectValidationError(f"Track {index} is invalid.")
                _strict_json_object(
                    item,
                    allowed=_SAVE_TRACK_FIELDS,
                    required=_SAVE_TRACK_FIELDS,
                    label=f"Track {index}",
                )
                if type(item["number"]) is not int or item["number"] <= 0:
                    raise ProjectValidationError(
                        f"Track {index} number must be a positive JSON integer."
                    )
                for time_key in ("start_seconds", "end_seconds"):
                    strict_finite_number(
                        item[time_key],
                        f"Track {index} {time_key.replace('_', ' ')}",
                    )
                try:
                    start_sample = item["start_sample"]
                    end_sample = item["end_sample"]
                except KeyError as exc:
                    raise ProjectValidationError(
                        f"Track {index} has invalid sample markers."
                    ) from exc
                if type(start_sample) is not int or type(end_sample) is not int:
                    raise ProjectValidationError(
                        f"Track {index} sample markers must be JSON integers."
                    )
                if start_sample < 0 or end_sample <= start_sample or end_sample > maximum_sample:
                    raise ProjectValidationError(
                        f"Track {index} sample markers are outside the source range."
                    )

                values: dict[str, str] = {}
                for key in editable_text:
                    value = item.get(key, "")
                    if not isinstance(value, str):
                        raise ProjectValidationError(f"Track {index} {key} must be text.")
                    value = value.strip()
                    if len(value) > 500:
                        raise ProjectValidationError(f"Track {index} {key} exceeds 500 characters.")
                    values[key] = value

                confidence_value = item.get("confidence", 0.0)
                try:
                    rendered_confidence = strict_finite_number(
                        confidence_value, f"Track {index} confidence"
                    )
                except ProjectValidationError:
                    raise ProjectValidationError(
                        f"Track {index} confidence must be a finite number between 0 and 1."
                    ) from None
                if not 0.0 <= rendered_confidence <= 1.0:
                    raise ProjectValidationError(
                        f"Track {index} confidence must be a finite number between 0 and 1."
                    )

                expected_duration_value = item.get("expected_duration_seconds")
                expected_duration: float | None = None
                if expected_duration_value is not None:
                    try:
                        expected_duration = strict_finite_number(
                            expected_duration_value,
                            f"Track {index} expected duration",
                        )
                    except ProjectValidationError as exc:
                        raise ProjectValidationError(
                            f"Track {index} expected duration must be positive and finite."
                        ) from exc
                    if expected_duration <= 0:
                        raise ProjectValidationError(
                            f"Track {index} expected duration must be positive and finite."
                        )

                updated_tracks.append(
                    Track(
                        number=index,
                        title=values["title"] or f"Track {index:02d}",
                        start_sample=start_sample,
                        end_sample=end_sample,
                        start_seconds=start_sample / sample_rate,
                        end_seconds=end_sample / sample_rate,
                        confidence=float(confidence_value),
                        artist=values["artist"],
                        album=values["album"],
                        album_artist=values["album_artist"],
                        year=values["year"],
                        genre=values["genre"],
                        side=values["side"],
                        expected_duration_seconds=expected_duration,
                        musicbrainz_recording_id=_optional_mbid(
                            item.get("musicbrainz_recording_id", ""),
                            f"Track {index} MusicBrainz recording ID",
                        ),
                        musicbrainz_track_id=_optional_mbid(
                            item.get("musicbrainz_track_id", ""),
                            f"Track {index} MusicBrainz track ID",
                        ),
                    )
                )
            project.tracks = updated_tracks

            metadata = payload.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ProjectValidationError("Project metadata must be an object.")
            updated_metadata: dict[str, str] = {}
            for key, value in metadata.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise ProjectValidationError("Project metadata keys and values must be text.")
                rendered_key = key.strip()
                rendered_value = value.strip()
                if not rendered_key:
                    raise ProjectValidationError("Project metadata keys cannot be empty.")
                if len(rendered_key) > 100 or len(rendered_value) > 500:
                    raise ProjectValidationError(
                        "Project metadata exceeds the supported text length."
                    )
                if rendered_value:
                    updated_metadata[rendered_key] = rendered_value
            project.metadata = updated_metadata
            _append_inferred_history(project, before_state)
            save_project(
                project,
                self.server.project_path,
                expected_existing_sha256=project_sha256,
            )
            project, project_sha256 = load_project_with_sha256(self.server.project_path)
            _source, source_receipt = self.server.verify_source(project, force_full=True)
            response_project = _project_payload(
                project,
                self.server.project_path,
                project_sha256,
                source_receipt,
            )
        self._json(
            {
                "ok": True,
                "updated_at": project.updated_at,
                "revision": project.revision,
                "project_sha256": project_sha256,
                "source_receipt": source_receipt,
                "project": response_project,
            }
        )

    def _metadata_search(self) -> None:
        payload = self._read_json()
        artist = str(payload.get("artist", "")).strip()[:500]
        album = str(payload.get("album", "")).strip()[:500]
        results = self.server.musicbrainz_client.search_releases(artist, album)
        self._json({"ok": True, "results": results})

    def _metadata_release(self) -> None:
        payload = self._read_json()
        release_id = str(payload.get("release_id", "")).strip()
        details = self.server.musicbrainz_client.get_release(release_id)
        project = load_project(self.server.project_path)
        ranked = find_track_selections(
            details,
            preferred_side=project.metadata.get("side", ""),
            expected_count=len(project.tracks),
        )
        # Mismatched counts remain visible so the browser can request a
        # reversible topology proposal instead of hiding a likely pressing.
        details["selections"] = ranked
        self._json({"ok": True, "release": details})

    def _metadata_apply(self) -> None:
        payload = self._read_json()
        expected_state = _expected_project_state(payload)
        release_id = str(payload.get("release_id", "")).strip()
        selection_key = str(payload.get("selection_key", "")).strip()
        download_artwork = payload.get("download_artwork", True)
        if not isinstance(download_artwork, bool):
            raise ProjectValidationError("Download artwork must be a boolean.")

        current = load_project(self.server.project_path)
        details = self.server.musicbrainz_client.get_release(release_id)
        selections = find_track_selections(
            details,
            preferred_side=current.metadata.get("side", ""),
            expected_count=len(current.tracks),
        )
        selection = next(
            (item for item in selections if str(item.get("key", "")) == selection_key),
            None,
        )
        if selection is None:
            raise ProjectValidationError("The selected release side no longer exists.")
        selected_tracks = selection.get("tracks")
        if not isinstance(selected_tracks, list) or len(selected_tracks) != len(current.tracks):
            raise ProjectValidationError(
                "The selected release side must have the same track count as this project."
            )

        artwork: dict[str, Any] | None = None
        warning = ""

        with self.server.operation_lock:
            project, project_sha256 = load_project_with_sha256(self.server.project_path)
            _assert_project_state(expected_state, project, project_sha256)
            self.server.verify_source(project, force_full=True)
            before_state = project.capture_state()
            if len(project.tracks) != len(selected_tracks):
                raise ProjectValidationError(
                    "The project track count changed while release metadata was loading."
                )
            if download_artwork:
                artwork_errors: list[str] = []
                release_group_id = str(details.get("release_group_id", "")).strip()
                if details.get("has_artwork"):
                    try:
                        artwork = self.server.cover_art_client.download_front_art(
                            release_id, size="1200"
                        )
                    except MetadataLookupError as exc:
                        artwork_errors.append(str(exc))
                if artwork is None and release_group_id:
                    try:
                        artwork = self.server.cover_art_client.download_release_group_front_art(
                            release_group_id, size="1200"
                        )
                    except MetadataLookupError as exc:
                        artwork_errors.append(str(exc))
                if artwork is None:
                    if artwork_errors:
                        warning = (
                            "Metadata applied, but artwork could not be downloaded: "
                            + artwork_errors[-1]
                        )
                    else:
                        warning = (
                            "Metadata applied; this MusicBrainz release and release group "
                            "have no front artwork."
                        )
            release_artist = str(details.get("artist", "")).strip()
            album = str(details.get("title", "")).strip()
            release_date = str(details.get("date", "")).strip()
            year = release_date[:4] if len(release_date) >= 4 else release_date
            genres = details.get("genres")
            genre = str(genres[0]).strip() if isinstance(genres, list) and genres else ""
            selection_side = str(selection.get("side") or "").strip()

            updated_tracks: list[Track] = []
            for original, matched in zip(project.tracks, selected_tracks, strict=True):
                if not isinstance(matched, dict):
                    raise ProjectValidationError("Release track metadata is invalid.")
                duration_value = matched.get("duration_seconds")
                try:
                    expected_duration = (
                        float(duration_value)
                        if duration_value not in (None, "")
                        else original.expected_duration_seconds
                    )
                except (TypeError, ValueError):
                    expected_duration = original.expected_duration_seconds
                matched_side = str(matched.get("side") or "").strip()
                updated_tracks.append(
                    Track(
                        number=original.number,
                        title=str(matched.get("title") or original.title).strip()[:500],
                        start_sample=original.start_sample,
                        end_sample=original.end_sample,
                        start_seconds=original.start_seconds,
                        end_seconds=original.end_seconds,
                        confidence=original.confidence,
                        artist=str(matched.get("artist") or release_artist).strip()[:500],
                        album=album[:500],
                        album_artist=release_artist[:500],
                        year=year[:500],
                        genre=genre[:500],
                        side=(selection_side or matched_side or original.side)[:500],
                        expected_duration_seconds=expected_duration,
                        musicbrainz_recording_id=_optional_mbid(
                            matched.get("recording_id"),
                            f"Track {original.number} MusicBrainz recording ID",
                        ),
                        musicbrainz_track_id=_optional_mbid(
                            matched.get("track_id"),
                            f"Track {original.number} MusicBrainz track ID",
                        ),
                    )
                )
            project.tracks = updated_tracks
            project.metadata.update(
                {
                    "artist": release_artist,
                    "album": album,
                    "album_artist": release_artist,
                    "year": year,
                    "genre": genre,
                    "side": selection_side or str(project.metadata.get("side", "")),
                    "musicbrainz_release_id": str(details.get("id", "")),
                    "musicbrainz_release_group_id": str(details.get("release_group_id", "")),
                    "musicbrainz_medium_position": str(selection.get("medium_position", "")),
                    "musicbrainz_provider": "MusicBrainz",
                    "release_country": str(details.get("country", "")),
                    "barcode": str(details.get("barcode", "")),
                    "label": str(details.get("label", "")),
                    "catalog_number": str(details.get("catalog_number", "")),
                }
            )
            project.metadata = {
                key: value for key, value in project.metadata.items() if value not in (None, "")
            }
            for key in [key for key in project.metadata if key.startswith("cover_art_")]:
                project.metadata.pop(key, None)
            if artwork is not None:
                project.metadata.update(
                    {
                        "cover_art_path": str(artwork["relative_path"]),
                        "cover_art_source": str(artwork["source_url"]),
                        "cover_art_mime_type": str(artwork["mime_type"]),
                        "cover_art_sha256": str(artwork["sha256"]),
                        "cover_art_size_bytes": str(artwork["size_bytes"]),
                    }
                )
            project.append_history(
                action="edit_metadata",
                summary="Applied reviewed MusicBrainz release metadata",
                before=before_state,
                after=project.capture_state(),
            )
            save_project(
                project,
                self.server.project_path,
                expected_existing_sha256=project_sha256,
            )
            project, project_sha256 = load_project_with_sha256(self.server.project_path)
            _source, source_receipt = self.server.verify_source(project, force_full=True)
            response_project = _project_payload(
                project,
                self.server.project_path,
                project_sha256,
                source_receipt,
            )
        self._json(
            {
                "ok": True,
                "project": response_project,
                "warning": warning,
            }
        )

    def _topology_propose(self) -> None:
        payload = self._read_json()
        required = {
            "expected_revision",
            "expected_project_sha256",
            "release_id",
            "selection_key",
        }
        _strict_json_object(
            payload,
            allowed=required,
            required=required,
            label="Topology proposal request",
        )
        expected_state = _expected_project_state(payload)
        release_id = _optional_mbid(payload["release_id"], "Release ID")
        if not release_id:
            raise ProjectValidationError("Release ID must be a valid UUID.")
        selection_key = payload["selection_key"]
        if (
            not isinstance(selection_key, str)
            or not selection_key.strip()
            or len(selection_key) > 500
        ):
            raise ProjectValidationError("Selection key must be non-empty text.")
        selection_key = selection_key.strip()

        current = load_project(self.server.project_path)
        details = self.server.musicbrainz_client.get_release(release_id)
        selections = find_track_selections(
            details,
            preferred_side=current.metadata.get("side", ""),
            expected_count=len(current.tracks),
        )
        selection = next(
            (item for item in selections if str(item.get("key", "")) == selection_key),
            None,
        )
        if selection is None:
            raise ProjectValidationError("The selected release topology no longer exists.")
        release_tracks = selection.get("tracks")
        if not isinstance(release_tracks, list):
            raise ProjectValidationError("The selected release has no valid track list.")

        with self.server.operation_lock:
            project, project_sha256 = load_project_with_sha256(self.server.project_path)
            _assert_project_state(expected_state, project, project_sha256)
            _source, source_receipt = self.server.verify_source(project)
            proposal = propose_topology_refit(project, release_tracks)
        self._json(
            {
                "ok": True,
                "release_id": release_id,
                "selection_key": selection_key,
                "selection_label": str(selection.get("label", "")),
                "current_track_count": len(project.tracks),
                "proposed_track_count": len(proposal["tracks"]),
                "proposal": proposal,
                "project_revision": project.revision,
                "project_sha256": project_sha256,
                "source_receipt": source_receipt,
            }
        )

    def _topology_apply(self) -> None:
        payload = self._read_json()
        required = {
            "expected_revision",
            "expected_project_sha256",
            "proposal",
        }
        _strict_json_object(
            payload,
            allowed=required,
            required=required,
            label="Topology apply request",
        )
        expected_state = _expected_project_state(payload)
        proposal = payload["proposal"]
        if type(proposal) is not dict:
            raise ProjectValidationError("Topology proposal must be a JSON object.")
        with self.server.operation_lock:
            project, project_sha256 = load_project_with_sha256(self.server.project_path)
            _assert_project_state(expected_state, project, project_sha256)
            self.server.verify_source(project, force_full=True)
            before_state = project.capture_state()
            before_count = len(project.tracks)
            project.tracks = tracks_from_topology_proposal(project, proposal)
            project.append_history(
                action="topology_refit",
                summary=(
                    f"Applied reviewed metadata topology {before_count} → "
                    f"{len(project.tracks)} tracks"
                ),
                before=before_state,
                after=project.capture_state(),
            )
            save_project(
                project,
                self.server.project_path,
                expected_existing_sha256=project_sha256,
            )
            project, project_sha256 = load_project_with_sha256(self.server.project_path)
            _source, source_receipt = self.server.verify_source(project, force_full=True)
            response_project = _project_payload(
                project,
                self.server.project_path,
                project_sha256,
                source_receipt,
            )
        self._json({"ok": True, "project": response_project})

    def _checkpoint(self) -> None:
        payload = self._read_json()
        required = {"expected_revision", "expected_project_sha256", "name"}
        _strict_json_object(
            payload,
            allowed=required,
            required=required,
            label="Checkpoint request",
        )
        expected_state = _expected_project_state(payload)
        name = payload["name"]
        if not isinstance(name, str):
            raise ProjectValidationError("Checkpoint name must be text.")
        name = name.strip()
        with self.server.operation_lock:
            project, project_sha256 = load_project_with_sha256(self.server.project_path)
            _assert_project_state(expected_state, project, project_sha256)
            self.server.verify_source(project, force_full=True)
            project.set_checkpoint(name)
            save_project(
                project,
                self.server.project_path,
                expected_existing_sha256=project_sha256,
            )
            project, project_sha256 = load_project_with_sha256(self.server.project_path)
            _source, source_receipt = self.server.verify_source(project, force_full=True)
            response_project = _project_payload(
                project,
                self.server.project_path,
                project_sha256,
                source_receipt,
            )
        self._json({"ok": True, "project": response_project})

    def _recognition_identify(self) -> None:
        payload = self._read_json()
        required = {
            "expected_revision",
            "expected_project_sha256",
            "expected_source_receipt",
            "track_number",
        }
        _strict_json_object(
            payload,
            allowed=required,
            required=required,
            label="Acoustic recognition request",
        )
        expected_state = _expected_project_state(payload)
        expected_receipt = _expected_source_receipt(payload)
        track_number = payload["track_number"]
        if type(track_number) is not int:
            raise ProjectValidationError("Track number must be an integer.")
        if not self.server.recognition_lock.acquire(blocking=False):
            raise ProjectValidationError(
                "Another acoustic identification request is already running."
            )
        try:
            with self.server.operation_lock:
                project, project_sha256 = load_project_with_sha256(self.server.project_path)
                _assert_project_state(expected_state, project, project_sha256)
                if not 1 <= track_number <= len(project.tracks):
                    raise ProjectValidationError("Track number is outside this project.")
                track = project.tracks[track_number - 1]
                source_snapshot, source_receipt = self.server.verified_source_snapshot(project)
                if source_receipt["receipt"] != expected_receipt:
                    raise _ProjectConflictError(
                        "The source verification receipt changed. "
                        "Reload before acoustic recognition."
                    )
                speed_state = project_speed_state(project)
                fingerprint_asetrate_hz, fingerprint_effective_speed_factor = (
                    speed_correction_details(
                        project.source.sample_rate,
                        speed_state.effective_speed_factor,
                    )
                )
                matches = self.server.recognition_provider.identify_track(
                    source_snapshot,
                    track.start_sample,
                    track.end_sample,
                    project.source.sample_rate,
                    source_speed_factor=speed_state.effective_speed_factor,
                )
                current, current_sha256 = load_project_with_sha256(self.server.project_path)
                _assert_project_state(expected_state, current, current_sha256)
                _snapshot, current_receipt = self.server.verified_source_snapshot(current)
                if current_receipt["receipt"] != expected_receipt:
                    raise _ProjectConflictError(
                        "The source verification changed while acoustic recognition was running."
                    )
        finally:
            self.server.recognition_lock.release()
        self._json(
            {
                "ok": True,
                "matches": [match.to_dict() for match in matches],
                "project_revision": project.revision,
                "project_sha256": project_sha256,
                "source_receipt": source_receipt,
                "track_region": {
                    "track_number": track_number,
                    "start_sample": track.start_sample,
                    "end_sample_exclusive": track.end_sample,
                    "sample_rate": project.source.sample_rate,
                    "speed_state_sha256": speed_state.sha256,
                    "requested_speed_factor": speed_state.effective_speed_factor,
                    "fingerprint_asetrate_hz": fingerprint_asetrate_hz,
                    "fingerprint_effective_speed_factor": (
                        fingerprint_effective_speed_factor
                    ),
                    "fingerprint_speed_transform": (
                        RECOGNITION_SPEED_TRANSFORM
                    ),
                },
            }
        )

    def _export(self) -> None:
        payload = self._read_json()
        identity_fields = {
            "expected_revision",
            "expected_project_sha256",
            "expected_source_receipt",
        }
        output_fields = {
            "output_dir",
            "formats",
            "overwrite",
            "flac_compression",
            "aac_bitrate",
            "source_speed_factor",
        }
        _strict_json_object(
            payload,
            allowed=identity_fields | output_fields,
            required=identity_fields | {"output_dir", "formats"},
            label="Direct export request",
        )
        expected_state = _expected_project_state(payload)
        expected_receipt = _expected_source_receipt(payload)
        output_value = payload["output_dir"]
        if not isinstance(output_value, str):
            raise ProjectValidationError("Output directory must be text.")
        output_value = output_value.strip()
        if len(output_value) > 32_768:
            raise ProjectValidationError("Output directory is too long.")
        formats = payload["formats"]
        if (
            type(formats) is not list
            or not formats
            or len(formats) > 2
            or any(type(value) is not str for value in formats)
            or any(value not in {"flac", "m4a"} for value in formats)
            or len(formats) != len(set(formats))
        ):
            raise ProjectValidationError(
                "Formats must be a non-empty array containing unique 'flac' and/or 'm4a' values."
            )
        overwrite = payload.get("overwrite", False)
        if type(overwrite) is not bool:
            raise ProjectValidationError("Overwrite must be a boolean.")
        flac_compression = payload.get("flac_compression", 8)
        if type(flac_compression) is not int or not 0 <= flac_compression <= 12:
            raise ProjectValidationError(
                "FLAC compression must be a JSON integer between 0 and 12."
            )
        aac_bitrate = _strict_aac_bitrate(payload.get("aac_bitrate", "256k"))
        source_speed_factor = payload.get("source_speed_factor")
        if source_speed_factor is not None:
            source_speed_factor = _finite_json_number(
                source_speed_factor,
                label="Source speed factor",
                minimum=0.25,
                maximum=2.0,
            )

        with self.server.operation_lock:
            project, project_sha256 = load_project_with_sha256(self.server.project_path)
            _assert_project_state(expected_state, project, project_sha256)
            _source, source_receipt = self.server.verify_source(project, force_full=True)
            if source_receipt["receipt"] != expected_receipt:
                raise _ProjectConflictError(
                    "The source verification receipt changed. Reload before export."
                )
            if output_value:
                output_dir = Path(output_value)
            else:
                output_dir = suggest_output_directory(project, self.server.project_path)
            if not output_dir.is_absolute():
                output_dir = self.server.project_path.parent / output_dir
            report = export_project(
                project,
                self.server.project_path,
                output_dir,
                formats=formats,
                overwrite=overwrite,
                flac_compression=flac_compression,
                aac_bitrate=aac_bitrate,
                source_speed_factor=source_speed_factor,
            )
            current, current_sha256 = load_project_with_sha256(self.server.project_path)
            _assert_project_state(expected_state, current, current_sha256)
            _source, current_receipt = self.server.verify_source(current, force_full=True)
            if current_receipt["receipt"] != expected_receipt:
                raise _ProjectConflictError(
                    "The source verification changed while export was running."
                )
        self._json(
            {
                "ok": True,
                "output_directory": report.output_directory,
                "manifest_path": report.manifest_path,
                "file_count": len(report.files),
                "project_revision": project.revision,
                "project_sha256": project_sha256,
                "source_receipt": source_receipt,
            }
        )

    def _evidence(self) -> None:
        payload = self._read_json()
        allowed = {"start_sample", "end_sample", "focus_sample"}
        unknown = set(payload) - allowed
        if unknown:
            raise ProjectValidationError(
                "Evidence request contains unsupported fields: "
                + ", ".join(sorted(str(value) for value in unknown))
            )
        for key in ("start_sample", "end_sample", "focus_sample"):
            if key not in payload or type(payload[key]) is not int:
                raise ProjectValidationError(
                    f"Evidence {key.replace('_', ' ')} must be a JSON integer."
                )
        request = self.server.begin_evidence_request()
        try:
            with self.server.operation_lock:
                project, project_sha256 = load_project_with_sha256(self.server.project_path)
                source_snapshot, before_receipt = self.server.verified_source_snapshot(
                    project,
                    force_full=False,
                    evidence_lease=True,
                )
            if request.cancelled():
                raise EvidenceRequestSuperseded(
                    "This evidence request was superseded by a newer selection."
                )
            cache_key = evidence_cache_key(
                project.source,
                start_sample=payload["start_sample"],
                end_sample=payload["end_sample"],
                focus_sample=payload["focus_sample"],
            )
            evidence = self.server.evidence_cache.get(cache_key)
            cache_miss = evidence is None
            if evidence is None:
                evidence = analyze_evidence_window(
                    source_snapshot.live_path,
                    project.source,
                    start_sample=payload["start_sample"],
                    end_sample=payload["end_sample"],
                    focus_sample=payload["focus_sample"],
                    source_snapshot=source_snapshot,
                    cancelled=request.cancelled,
                )
            if request.cancelled():
                raise EvidenceRequestSuperseded(
                    "This evidence request was superseded by a newer selection."
                )
            with self.server.operation_lock:
                current, current_sha256 = load_project_with_sha256(self.server.project_path)
                if current.revision != project.revision or current_sha256 != project_sha256:
                    raise _ProjectConflictError(
                        "The project changed while evidence was being decoded."
                    )
                _snapshot, after_receipt = self.server.verified_source_snapshot(
                    current,
                    force_full=False,
                    evidence_lease=True,
                )
                if after_receipt["receipt"] != before_receipt["receipt"]:
                    raise ProjectValidationError(
                        "The source changed while evidence was being decoded."
                    )
            if request.cancelled():
                raise EvidenceRequestSuperseded(
                    "This evidence request was superseded by a newer selection."
                )
            if cache_miss:
                self.server.evidence_cache.put(cache_key, evidence)
            evidence["project_revision"] = project.revision
            evidence["project_sha256"] = project_sha256
            evidence["source_receipt"] = after_receipt
            if request.cancelled():
                raise EvidenceRequestSuperseded(
                    "This evidence request was superseded by a newer selection."
                )
            self._json(evidence)
        finally:
            self.server.finish_evidence_request(request)


def _ipv4_server_endpoint(server: ReviewServer) -> tuple[str, int]:
    """Return the validated two-field address guaranteed by this IPv4 server."""

    address = server.server_address
    if server.address_family != socket.AF_INET or not isinstance(address, tuple):
        raise GrooveSerpentError("The review server did not bind an IPv4 endpoint.")
    if len(address) != 2:
        raise GrooveSerpentError("The review server returned an invalid IPv4 endpoint.")
    host, port = address
    if not isinstance(host, str) or type(port) is not int:
        raise GrooveSerpentError("The review server returned an invalid IPv4 endpoint.")
    return host, port


def serve_project(
    project_path: Path,
    *,
    port: int = 0,
    open_browser: bool = True,
    endpoint_proposal_path: Path | None = None,
) -> int:
    if type(port) is not int or not 0 <= port <= 65_535:
        raise ProjectValidationError("The review port must be a JSON integer from 0 to 65535.")
    project_path = project_path.expanduser().resolve()
    load_project(project_path)  # Validate before opening the server.
    server = ReviewServer(
        ("127.0.0.1", port),
        project_path,
        endpoint_proposal_path=endpoint_proposal_path,
    )
    _host, selected_port = _ipv4_server_endpoint(server)
    url = f"{server.session_auth.origin(port=selected_port)}/"
    bootstrap_url = server.session_auth.bootstrap_url(port=selected_port)
    print(f"Reviewing {project_path.name}")
    print(f"Local review page: {url}")
    if not open_browser:
        print(
            "One-time session bootstrap URL (keep this credential private): "
            f"{bootstrap_url}"
        )
    print("Press Ctrl+C to stop the review server.")
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(bootstrap_url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nReview server stopped.")
    finally:
        server.server_close()
    return 0
