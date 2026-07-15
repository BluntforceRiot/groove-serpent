"""Execute an approved album-publication plan as one atomic directory commit.

The plan is authority only after every referenced byte, side identity, selected
speed state, restoration outcome, and production tool binding has been checked
against current local state.  Stored command text is never executed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .atomic_create import rename_no_replace
from .album import (
    AlbumProject,
    AlbumSide,
    _side_identity_status,
    _validated_artwork,
    load_album_project_with_sha256,
)
from .album_publication_plan import (
    AlbumPublicationPlan,
    ProcessingNode,
    PublicationSide,
    RestorationNoDerivativeBinding,
    RestorationRenderBinding,
    SideIdentity,
    SpeedSelection,
    load_album_publication_plan_with_sha256,
    verify_album_publication_plan_identity,
)
from .album_publication_navigation import (
    ALBUM_PUBLICATION_CHAPTERS_NAME,
    ALBUM_PUBLICATION_CHAPTERS_SCHEMA,
    ALBUM_PUBLICATION_CUE_NAME,
    build_album_chapters,
    navigation_sides_from_publication,
    render_album_cue,
)
from .album_publication_policy import (
    PublicationSettings,
    ToolObservations,
    observe_publication_tools,
    speed_correction_details,
    validate_operation_tool_binding,
)
from .cache_storage import ensure_free_space
from .errors import ExportError, GrooveSerpentError, ProjectValidationError
from .exporter import (
    _complete_decode,
    _decoded_pcm_sha256,
    _probe_m4a_presentation_sample_count,
    _probe_exact_audio_stream,
    _resolve_portable_export_path,
    _speed_corrected_sample,
    render_verified_track,
    sanitize_filename,
)
from .media import find_tool
from .models import Project, Track
from .portable_names import (
    PortablePathError,
    portable_path_entry_exists,
    resolve_portable_path,
)
from .publication import (
    FileReceipt,
    assert_file_receipt,
    capture_file_receipt,
    stage_verified_copy,
)
from .restoration_catalog import (
    RestorationArtifact,
    RestorationCatalog,
    discover_restoration_catalog,
)
from .subprocess_policy import run_bounded_capture


LEGACY_ALBUM_PUBLICATION_MANIFEST_SCHEMA = "groove-serpent.album-publication-manifest/1"
ALBUM_PUBLICATION_MANIFEST_SCHEMA = "groove-serpent.album-publication-manifest/2"
ALBUM_PUBLICATION_JOURNAL_SCHEMA = "groove-serpent.album-publication-journal/1"
_MANIFEST_NAME = "groove-serpent-album-publication.json"
_JOURNAL_NAME = "groove-serpent-publication-journal.json"
_STAGE_PREFIX = ".groove-serpent-album-publication-"
_STAGE_SUFFIX = ".partial"
_REPARSE_POINT = 0x400
_FILE_OVERHEAD_BYTES = 1024 * 1024
_MAX_STAGE_ENTRIES = 100_000
_MAX_STAGE_DEPTH = 32
_MAX_EMBEDDED_ARTWORK_BYTES = 25 * 1024 * 1024
_SEMANTIC_AUDIO_TAGS = {
    "album",
    "album_artist",
    "artist",
    "barcode",
    "catalog_number",
    "comment",
    "date",
    "disc",
    "genre",
    "groove_serpent_asetrate_hz",
    "groove_serpent_effective_speed_factor",
    "groove_serpent_source_speed_factor",
    "groove_serpent_speed_correction",
    "grouping",
    "musicbrainz_albumid",
    "musicbrainz_recordingid",
    "musicbrainz_releasegroupid",
    "musicbrainz_trackid",
    "publisher",
    "title",
    "track",
    "tracktotal",
    "vinyl_side",
}


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    device: int
    inode: int
    file_type: int
    birth_ns: int | None
    file_attributes: int | None


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    """One committed publication artifact."""

    profile: str
    role: str
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class AlbumPublicationExecutionReport:
    """The immutable result of one successful outer-directory commit."""

    output_directory: str
    manifest_path: str
    plan_sha256: str
    artifacts: tuple[PublishedArtifact, ...]


@dataclass(frozen=True, slots=True)
class AlbumPublicationPreflightReport:
    """Portable immutable identities from one read-only execution preflight."""

    plan_sha256: str
    album_sha256: str
    selected_profiles: tuple[str, ...]
    side_count: int


@dataclass(frozen=True, slots=True)
class _RestorationLease:
    kind: str
    manifest_path: Path
    manifest_receipt: FileReceipt
    audio_path: Path | None
    audio_receipt: FileReceipt | None
    artifact: RestorationArtifact
    catalog: RestorationCatalog


@dataclass(frozen=True, slots=True)
class _SideLease:
    planned: PublicationSide
    album_side: AlbumSide
    project_path: Path
    project: Project
    project_receipt: FileReceipt
    source_path: Path
    source_receipt: FileReceipt
    identity: SideIdentity
    speed: SpeedSelection
    music_start: int
    music_end: int
    restoration: _RestorationLease | None


@dataclass(frozen=True, slots=True)
class _ExecutionLease:
    plan_path: Path
    plan: AlbumPublicationPlan
    plan_receipt: FileReceipt
    raw_plan_sha256: str
    album_path: Path
    album: AlbumProject
    album_receipt: FileReceipt
    sides: tuple[_SideLease, ...]
    artwork_path: Path | None
    artwork_receipt: FileReceipt | None
    observations: ToolObservations
    restoration_mode: str
    settings: PublicationSettings


@dataclass(frozen=True, slots=True)
class _StagedSide:
    lease: _SideLease
    project_snapshot: Path
    source_snapshot: Path
    source_snapshot_receipt: FileReceipt
    restoration_audio_snapshot: Path | None


@dataclass(frozen=True, slots=True)
class _SourceObject:
    """One deterministic full-capture identity shared by one or more sides."""

    object_id: str
    first_side_order: int
    source_path: Path
    source_receipt: FileReceipt
    sides: tuple[_SideLease, ...]


@dataclass(frozen=True, slots=True)
class _LosslessTrack:
    side_label: str
    album_track_number: int
    track: Track
    path: Path
    sample_count: int


def _source_objects(sides: Iterable[_SideLease]) -> tuple[_SourceObject, ...]:
    """Group sides only when their exact source bytes have one identity."""

    grouped: list[tuple[_SideLease, list[_SideLease]]] = []
    by_identity: dict[tuple[str, int], int] = {}
    for side in sorted(sides, key=lambda value: value.planned.order):
        identity = (side.source_receipt.sha256, side.source_receipt.size_bytes)
        index = by_identity.get(identity)
        if index is None:
            by_identity[identity] = len(grouped)
            grouped.append((side, [side]))
        else:
            grouped[index][1].append(side)
    return tuple(
        _SourceObject(
            object_id=(f"source-{index:02d}-{first.source_receipt.sha256[:12]}"),
            first_side_order=first.planned.order,
            source_path=first.source_path,
            source_receipt=first.source_receipt,
            sides=tuple(bound_sides),
        )
        for index, (first, bound_sides) in enumerate(grouped, start=1)
    )


def _notify(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _inject_fault(
    fault_injector: Callable[[str], None] | None,
    boundary: str,
) -> None:
    if fault_injector is not None:
        fault_injector(boundary)


def _absolute_without_final_resolution(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _directory_identity(path: Path, *, label: str) -> _DirectoryIdentity:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ExportError(f"{label} could not be inspected: {exc}") from exc
    attributes_value = getattr(metadata, "st_file_attributes", None)
    attributes = int(attributes_value) if attributes_value is not None else None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or (attributes or 0) & _REPARSE_POINT
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise ExportError(f"{label} is not one ordinary directory.")
    birth_value = getattr(metadata, "st_birthtime_ns", None)
    return _DirectoryIdentity(
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        file_type=stat.S_IFMT(metadata.st_mode),
        birth_ns=int(birth_value) if birth_value is not None else None,
        file_attributes=attributes,
    )


def _assert_regular_no_reparse(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ExportError(f"{label} could not be inspected: {exc}") from exc
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if (
        stat.S_ISLNK(metadata.st_mode)
        or attributes & _REPARSE_POINT
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise ExportError(f"{label} must be an ordinary non-reparse file.")


def _atomic_no_replace_directory(source: Path, destination: Path) -> None:
    """Rename one directory atomically while refusing an existing destination."""

    if source.parent != destination.parent:
        raise ExportError("Atomic publication commit requires one parent directory.")
    try:
        rename_no_replace(source, destination)
    except FileExistsError as exc:
        raise ExportError("Publication output already exists.") from exc
    except (OSError, ValueError) as exc:
        raise ExportError(f"Atomic no-replace publication commit failed: {exc}") from exc


def _resolve_plan_reference(plan_path: Path, reference: str, label: str) -> Path:
    candidate = Path(reference)
    if (
        not reference
        or reference != reference.strip()
        or candidate.is_absolute()
        or candidate.drive
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ProjectValidationError(
            f"{label} must be a contained relative publication-plan reference."
        )
    root = plan_path.parent.resolve()
    try:
        resolution = resolve_portable_path(root / candidate)
        if not resolution.entry_exists:
            raise ExportError(f"{label} does not exist.")
        resolved = _absolute_without_final_resolution(resolution.path)
        resolved.parent.resolve().relative_to(root)
    except (OSError, PortablePathError, RuntimeError, ValueError) as exc:
        raise ProjectValidationError(f"{label} escapes the plan folder.") from exc
    _assert_regular_no_reparse(resolved, label=label)
    return resolved


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return left.resolve() == right.resolve()


def _side_identity(
    project: Project,
    project_sha256: str,
    source_sha256: str,
    project_speed_state_sha256: str,
) -> SideIdentity:
    return SideIdentity(
        project_revision=project.revision,
        project_sha256=project_sha256,
        editable_state_sha256=project.state_sha256,
        source_sha256=source_sha256,
        project_speed_state_sha256=project_speed_state_sha256,
    )


def _validate_track_topology(project: Project, label: str) -> tuple[int, int]:
    if not project.tracks:
        raise ExportError(f"Side {label} has no tracks to publish.")
    previous_end: int | None = None
    for track in project.tracks:
        if track.end_sample <= track.start_sample:
            raise ExportError(f"Side {label} has an empty track range.")
        if previous_end is not None and track.start_sample != previous_end:
            raise ExportError(
                f"Side {label} track markers are not sample-adjacent. "
                "Review the boundary before publication."
            )
        previous_end = track.end_sample
    return project.tracks[0].start_sample, project.tracks[-1].end_sample


def _find_exact_artifact(
    artifacts: Iterable[RestorationArtifact],
    *,
    kind: str,
    manifest_path: Path,
    manifest_sha256: str,
) -> RestorationArtifact:
    matches = [
        item
        for item in artifacts
        if item.kind == kind
        and _same_file(item.manifest_path, manifest_path)
        and item.manifest_sha256 == manifest_sha256
    ]
    if len(matches) != 1:
        raise ExportError(f"The bound {kind} artifact is not one exact current catalog artifact.")
    return matches[0]


def _lease_render(
    plan_path: Path,
    project_path: Path,
    project: Project,
    source_receipt: FileReceipt,
    binding: RestorationRenderBinding,
) -> _RestorationLease:
    manifest_path = _resolve_plan_reference(
        plan_path,
        binding.manifest_reference,
        "Restoration render manifest",
    )
    audio_path = _resolve_plan_reference(
        plan_path,
        binding.audio_reference,
        "Restoration render audio",
    )
    manifest_receipt = capture_file_receipt(
        manifest_path,
        label="Restoration render manifest",
    )
    audio_receipt = capture_file_receipt(
        audio_path,
        label="Restoration render audio",
    )
    if manifest_receipt.sha256 != binding.manifest_sha256:
        raise ExportError("The restoration render manifest changed after approval.")
    if audio_receipt.sha256 != binding.audio_sha256:
        raise ExportError("The restoration render audio changed after approval.")
    catalog = discover_restoration_catalog(
        manifest_path.parent.parent,
        project_path,
        verified_source_sha256=source_receipt.sha256,
    )
    artifact = _find_exact_artifact(
        catalog.artifacts,
        kind="render",
        manifest_path=manifest_path,
        manifest_sha256=manifest_receipt.sha256,
    )
    selected = catalog.latest_chain().render
    if selected is None or selected.artifact_id != artifact.artifact_id:
        raise ExportError("The bound restoration render is no longer the latest current chain.")
    if not any(
        _same_file(item.path, audio_path)
        and item.sha256 == audio_receipt.sha256
        and item.size_bytes == audio_receipt.size_bytes
        for item in artifact.files
    ):
        raise ExportError(
            "The bound restoration audio is not a verified file of the render artifact."
        )
    music_start, music_end = _validate_track_topology(project, "restoration")
    details = _probe_exact_audio_stream(audio_path)
    expected_bits = 24 if (project.source.bits_per_raw_sample or 16) > 16 else 16
    expected = {
        "codec_name": "flac",
        "sample_rate": project.source.sample_rate,
        "channels": project.source.channels,
        "bits_per_raw_sample": expected_bits,
        "exact_sample_count": music_end - music_start,
    }
    for key, value in expected.items():
        if details.get(key) != value:
            raise ExportError(f"Restoration render audio has unexpected {key.replace('_', ' ')}.")
    _complete_decode(audio_path)
    return _RestorationLease(
        kind="render",
        manifest_path=manifest_path,
        manifest_receipt=manifest_receipt,
        audio_path=audio_path,
        audio_receipt=audio_receipt,
        artifact=artifact,
        catalog=catalog,
    )


def _lease_no_derivative(
    plan_path: Path,
    project_path: Path,
    source_receipt: FileReceipt,
    binding: RestorationNoDerivativeBinding,
) -> _RestorationLease:
    scan_path = _resolve_plan_reference(
        plan_path,
        binding.scan_reference,
        "No-derivative restoration scan",
    )
    scan_receipt = capture_file_receipt(
        scan_path,
        label="No-derivative restoration scan",
    )
    if scan_receipt.sha256 != binding.scan_sha256:
        raise ExportError("The no-derivative restoration scan changed after approval.")
    catalog = discover_restoration_catalog(
        scan_path.parent,
        project_path,
        verified_source_sha256=source_receipt.sha256,
    )
    artifact = _find_exact_artifact(
        catalog.artifacts,
        kind="scan",
        manifest_path=scan_path,
        manifest_sha256=scan_receipt.sha256,
    )
    selected = catalog.latest_chain()
    if selected.scan is None or selected.scan.artifact_id != artifact.artifact_id:
        raise ExportError("The bound reviewed-clean scan is no longer the latest current chain.")
    if selected.recipe is not None or selected.render is not None:
        raise ExportError("The bound reviewed-clean scan now has a derivative decision chain.")
    coverage = artifact.payload.get("coverage")
    if not isinstance(coverage, dict):
        raise ExportError("The no-derivative scan has no validated coverage ledger.")
    expected: Mapping[str, Any] = {
        "restoration_status": binding.restoration_status,
        "scan_range_covers_music": binding.scan_range_covers_music,
        "candidate_scan_truncated": binding.candidate_scan_truncated,
        "retained_candidates": binding.retained_candidates,
    }
    if any(coverage.get(key) != value for key, value in expected.items()):
        raise ExportError("The no-derivative scan coverage no longer matches its approved binding.")
    return _RestorationLease(
        kind="clean",
        manifest_path=scan_path,
        manifest_receipt=scan_receipt,
        audio_path=None,
        audio_receipt=None,
        artifact=artifact,
        catalog=catalog,
    )


def _validate_node_binding(
    node: ProcessingNode,
    *,
    side_by_label: Mapping[str, _SideLease],
    settings: PublicationSettings,
    observations: ToolObservations,
    restoration_mode: str,
) -> None:
    if node.operation == "correct-speed-side":
        if node.side_label is None or node.side_label not in side_by_label:
            raise ExportError("A speed-correction node names an unknown side.")
        side = side_by_label[node.side_label]
        validate_operation_tool_binding(
            node.operation,
            node.tool,
            settings,
            observations,
            source_sample_rate=side.project.source.sample_rate,
            requested_speed_factor=side.speed.selected_effective_speed_factor,
            restoration_mode=restoration_mode,
        )
    else:
        validate_operation_tool_binding(
            node.operation,
            node.tool,
            settings,
            observations,
        )


def _settings_from_plan(plan: AlbumPublicationPlan) -> PublicationSettings:
    flac_values: set[int] = set()
    aac_values: set[int] = set()
    for node in plan.nodes:
        configuration = node.tool.configuration
        if node.operation == "assemble-restored":
            value = configuration.get("clean_side_flac_compression")
            if type(value) is not int:
                raise ExportError("The restored assembly binding has no integer FLAC setting.")
            flac_values.add(value)
        elif node.operation == "encode-lossless":
            value = configuration.get("flac_compression")
            if type(value) is not int:
                raise ExportError("The lossless encoding binding has no integer FLAC setting.")
            flac_values.add(value)
        elif node.operation == "encode-portable":
            value = configuration.get("bitrate_kbps")
            if type(value) is not int:
                raise ExportError("The portable encoding binding has no integer AAC setting.")
            aac_values.add(value)
    if len(flac_values) > 1 or len(aac_values) > 1:
        raise ExportError("Publication nodes bind inconsistent encoding settings.")
    settings = PublicationSettings(
        flac_compression=next(iter(flac_values), 8),
        aac_bitrate_kbps=next(iter(aac_values), 256),
    )
    settings.validate()
    return settings


def _capture_execution_lease(
    plan_path: Path,
    requested_settings: PublicationSettings | None,
) -> _ExecutionLease:
    plan_path = _absolute_without_final_resolution(plan_path)
    _assert_regular_no_reparse(plan_path, label="Album publication plan")
    plan_receipt = capture_file_receipt(plan_path, label="Album publication plan")
    plan, raw_plan_sha256 = load_album_publication_plan_with_sha256(plan_path)
    if raw_plan_sha256 != plan_receipt.sha256:
        raise ExportError("The publication plan changed while it was loaded.")
    settings = _settings_from_plan(plan)
    if requested_settings is not None:
        requested_settings.validate()
        if requested_settings != settings:
            raise ExportError("Caller publication settings differ from the approved plan bindings.")

    album_path = _resolve_plan_reference(
        plan_path,
        plan.album_reference,
        "Album project",
    )
    album_receipt = capture_file_receipt(album_path, label="Album project")
    album, album_sha256 = load_album_project_with_sha256(album_path)
    if album_sha256 != album_receipt.sha256 or album_sha256 != plan.album_sha256:
        raise ExportError("The album project no longer matches the publication plan.")
    if len(album.sides) != len(plan.sides):
        raise ExportError("The album and publication plan have different side counts.")

    restoration_mode = (
        "reviewed"
        if any(
            side.restoration_render is not None or side.restoration_no_derivative is not None
            for side in plan.sides
        )
        else "none"
    )
    side_leases: list[_SideLease] = []
    identities: dict[str, SideIdentity] = {}
    speeds: dict[str, SpeedSelection] = {}
    for planned, album_side in zip(plan.sides, album.sides, strict=True):
        if planned.order != album_side.order or planned.label != album_side.label:
            raise ExportError("Publication side order or labels no longer match the album.")
        planned_project = _resolve_plan_reference(
            plan_path,
            planned.project_reference,
            f"Side {planned.label} project",
        )
        album_project = _resolve_plan_reference(
            album_path,
            album_side.project,
            f"Side {planned.label} album project reference",
        )
        if not _same_file(planned_project, album_project):
            raise ExportError(f"Side {planned.label} plan and album project references differ.")
        status, current = _side_identity_status(album_side, album_path)
        if status.get("ready_for_export") is not True:
            drift = status.get("drift")
            details = ", ".join(drift) if isinstance(drift, list) else "identity drift"
            raise ExportError(f"Side {planned.label} is not export-ready: {details}.")
        (
            project_path,
            project,
            project_sha256,
            source_path,
            source_sha256,
            project_speed,
        ) = current
        _assert_regular_no_reparse(
            project_path,
            label=f"Side {planned.label} project",
        )
        _assert_regular_no_reparse(
            source_path,
            label=f"Side {planned.label} source",
        )
        project_receipt = capture_file_receipt(
            project_path,
            label=f"Side {planned.label} project",
        )
        source_receipt = capture_file_receipt(
            source_path,
            label=f"Side {planned.label} source",
        )
        if project_receipt.sha256 != project_sha256:
            raise ExportError(f"Side {planned.label} project changed during inspection.")
        if source_receipt.sha256 != source_sha256:
            raise ExportError(f"Side {planned.label} source changed during inspection.")
        if project.source.bits_per_raw_sample not in {16, 24}:
            raise ExportError(f"Side {planned.label} must have known 16- or 24-bit PCM precision.")
        identity = _side_identity(
            project,
            project_sha256,
            source_sha256,
            project_speed.sha256,
        )
        speed = SpeedSelection(
            selected_speed_state_sha256=album_side.speed.state_sha256,
            selected_effective_speed_factor=album_side.effective_speed_factor,
        ).normalized()
        music_start, music_end = _validate_track_topology(project, planned.label)
        restoration: _RestorationLease | None = None
        if planned.restoration_render is not None:
            restoration = _lease_render(
                plan_path,
                project_path,
                project,
                source_receipt,
                planned.restoration_render,
            )
        elif planned.restoration_no_derivative is not None:
            restoration = _lease_no_derivative(
                plan_path,
                project_path,
                source_receipt,
                planned.restoration_no_derivative,
            )
        lease = _SideLease(
            planned=planned,
            album_side=album_side,
            project_path=project_path,
            project=project,
            project_receipt=project_receipt,
            source_path=source_path,
            source_receipt=source_receipt,
            identity=identity,
            speed=speed,
            music_start=music_start,
            music_end=music_end,
            restoration=restoration,
        )
        side_leases.append(lease)
        identities[planned.label] = identity
        speeds[planned.label] = speed

    verification = verify_album_publication_plan_identity(
        plan,
        current_album_sha256=album_receipt.sha256,
        current_side_identities=identities,
        current_side_speed_selections=speeds,
    )
    if not verification.ok:
        codes = ", ".join(item.code for item in verification.mismatches)
        raise ExportError(f"Publication plan identity verification failed: {codes}.")

    artwork_path: Path | None = None
    artwork_receipt: FileReceipt | None = None
    if album.artwork is not None:
        artwork_path = _resolve_plan_reference(
            album_path,
            album.artwork.path,
            "Album artwork",
        )
        artwork_sha256 = _validated_artwork(artwork_path)
        artwork_receipt = capture_file_receipt(artwork_path, label="Album artwork")
        if artwork_sha256 != album.artwork.sha256 or artwork_receipt.sha256 != album.artwork.sha256:
            raise ExportError("The album artwork no longer matches its approval.")

    observations = observe_publication_tools()
    by_label = {side.planned.label: side for side in side_leases}
    for node in plan.nodes:
        _validate_node_binding(
            node,
            side_by_label=by_label,
            settings=settings,
            observations=observations,
            restoration_mode=restoration_mode,
        )
    return _ExecutionLease(
        plan_path=plan_path,
        plan=plan,
        plan_receipt=plan_receipt,
        raw_plan_sha256=raw_plan_sha256,
        album_path=album_path,
        album=album,
        album_receipt=album_receipt,
        sides=tuple(side_leases),
        artwork_path=artwork_path,
        artwork_receipt=artwork_receipt,
        observations=observations,
        restoration_mode=restoration_mode,
        settings=settings,
    )


def _receipt_payload(receipt: FileReceipt) -> dict[str, Any]:
    return {"sha256": receipt.sha256, "size_bytes": receipt.size_bytes}


def _directory_identity_payload(identity: _DirectoryIdentity) -> dict[str, Any]:
    return asdict(identity)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    text = (
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
        + "\n"
    )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_new_text(path: Path, text: str, *, label: str) -> None:
    """Write one fsynced private-stage artifact without replacing any entry."""

    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ExportError(f"{label} unexpectedly already exists in the private stage.") from exc
    except OSError as exc:
        raise ExportError(f"{label} could not be written safely: {exc}") from exc


def _write_navigation_artifacts(
    lease: _ExecutionLease,
    stage: Path,
    inventory: list[dict[str, Any]],
    archival_sources: Mapping[str, Any],
) -> None:
    raw_bindings = archival_sources.get("side_bindings", [])
    archival_navigation_bindings = {
        str(item["side_label"]): str(item["source_object_id"]) for item in raw_bindings
    }
    basis_profile, navigation_sides = navigation_sides_from_publication(
        album=lease.album,
        projects_by_label={side.planned.label: side.project for side in lease.sides},
        selected_profiles=lease.plan.selected_profiles,
        inventory=inventory,
        archival_source_bindings=archival_navigation_bindings,
    )
    chapters = build_album_chapters(
        plan_sha256=lease.plan.plan_sha256,
        album_sha256=lease.album_receipt.sha256,
        basis_profile=basis_profile,
        metadata=dict(lease.album.metadata),
        sides=navigation_sides,
    )
    chapters_path = stage / ALBUM_PUBLICATION_CHAPTERS_NAME
    chapters_text = (
        json.dumps(
            chapters,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
        + "\n"
    )
    _write_new_text(chapters_path, chapters_text, label="Exact album chapters")
    inventory.append(
        _inventory_item(
            stage,
            chapters_path,
            profile=basis_profile,
            role="exact-chapters",
            schema=ALBUM_PUBLICATION_CHAPTERS_SCHEMA,
            precision="exact-integer-sample-positions",
        )
    )

    cue_path = stage / ALBUM_PUBLICATION_CUE_NAME
    _write_new_text(
        cue_path,
        render_album_cue(metadata=dict(lease.album.metadata), sides=navigation_sides),
        label="Approximate album CUE",
    )
    inventory.append(
        _inventory_item(
            stage,
            cue_path,
            profile=basis_profile,
            role="approximate-cue",
            timebase_frames_per_second=75,
            precision="approximate-rounded-navigation-indexes",
        )
    )


def _journal(
    stage: Path,
    state: str,
    plan_sha256: str,
    *,
    operation_id: str,
    intended_output_name: str,
    stage_identity: _DirectoryIdentity,
) -> None:
    _write_json(
        stage / _JOURNAL_NAME,
        {
            "schema": ALBUM_PUBLICATION_JOURNAL_SCHEMA,
            "state": state,
            "plan_sha256": plan_sha256,
            "operation_id": operation_id,
            "original_stage_name": stage.name,
            "intended_output_name": intended_output_name,
            "stage_identity": _directory_identity_payload(stage_identity),
        },
    )


def _stage_snapshots(
    lease: _ExecutionLease,
    stage: Path,
) -> tuple[tuple[_StagedSide, ...], Path | None]:
    provenance = stage / "provenance"
    provenance.mkdir()
    stage_verified_copy(
        lease.plan_path,
        provenance / "publication-plan.json",
        lease.plan_receipt,
        label="Publication plan",
    )
    stage_verified_copy(
        lease.album_path,
        provenance / "album-project.json",
        lease.album_receipt,
        label="Album project",
    )
    side_root = provenance / "sides"
    side_root.mkdir()
    work = stage / ".work"
    work.mkdir()
    input_root = work / "input-snapshots"
    input_root.mkdir()
    input_source_root = input_root / "sources"
    input_source_root.mkdir()
    input_side_root = input_root / "sides"
    input_side_root.mkdir()
    source_snapshots: dict[str, tuple[Path, FileReceipt]] = {}
    for source_object in _source_objects(lease.sides):
        source_snapshot = input_source_root / _source_object_name(source_object)
        source_snapshot_receipt = stage_verified_copy(
            source_object.source_path,
            source_snapshot,
            source_object.source_receipt,
            label=f"Archival source object {source_object.object_id}",
        )
        for side in source_object.sides:
            source_snapshots[side.planned.label] = (
                source_snapshot,
                source_snapshot_receipt,
            )
    staged_sides: list[_StagedSide] = []
    for side in lease.sides:
        provenance_root = side_root / f"{side.planned.order:02d}"
        provenance_root.mkdir()
        working_root = input_side_root / f"{side.planned.order:02d}"
        working_root.mkdir()
        project_snapshot = provenance_root / "project.groove.json"
        stage_verified_copy(
            side.project_path,
            project_snapshot,
            side.project_receipt,
            label=f"Side {side.planned.label} project",
        )
        source_snapshot, source_snapshot_receipt = source_snapshots[side.planned.label]
        restoration_audio_snapshot: Path | None = None
        if side.restoration is not None:
            restoration_provenance = provenance_root / "restoration"
            restoration_provenance.mkdir()
            manifest_name = (
                "render-manifest.json" if side.restoration.kind == "render" else "clean-scan.json"
            )
            stage_verified_copy(
                side.restoration.manifest_path,
                restoration_provenance / manifest_name,
                side.restoration.manifest_receipt,
                label=f"Side {side.planned.label} restoration manifest",
            )
            if (
                side.restoration.audio_path is not None
                and side.restoration.audio_receipt is not None
            ):
                restoration_work = working_root / "restoration"
                restoration_work.mkdir()
                restoration_audio_snapshot = restoration_work / "restored.flac"
                stage_verified_copy(
                    side.restoration.audio_path,
                    restoration_audio_snapshot,
                    side.restoration.audio_receipt,
                    label=f"Side {side.planned.label} restoration audio",
                )
        staged_sides.append(
            _StagedSide(
                lease=side,
                project_snapshot=project_snapshot,
                source_snapshot=source_snapshot,
                source_snapshot_receipt=source_snapshot_receipt,
                restoration_audio_snapshot=restoration_audio_snapshot,
            )
        )

    artwork_snapshot: Path | None = None
    if lease.artwork_path is not None and lease.artwork_receipt is not None:
        artwork_root = stage / "artwork"
        artwork_root.mkdir()
        artwork_snapshot = artwork_root / f"cover{lease.artwork_path.suffix.casefold()}"
        stage_verified_copy(
            lease.artwork_path,
            artwork_snapshot,
            lease.artwork_receipt,
            label="Album artwork",
        )
    return tuple(staged_sides), artwork_snapshot


def _side_name(side: _SideLease, suffix: str = ".flac") -> str:
    prefix = f"{side.planned.order:02d}-"
    label = sanitize_filename(
        side.planned.label,
        f"Side-{side.planned.order:02d}",
        prefix=prefix,
        suffix=suffix,
    )
    return f"{prefix}{label}{suffix}"


def _source_object_filename(object_id: str, source_path: Path) -> str:
    suffix = source_path.suffix.casefold() or ".audio"
    source = sanitize_filename(
        source_path.stem,
        "capture",
        prefix=f"{object_id}-",
        suffix=suffix,
    )
    return f"{object_id}-{source}{suffix}"


def _source_object_name(source_object: _SourceObject) -> str:
    return _source_object_filename(
        source_object.object_id,
        Path(source_object.sides[0].project.source.filename),
    )


def _album_metadata(lease: _ExecutionLease) -> dict[str, str]:
    return dict(lease.album.metadata)


def _side_track(
    side: _SideLease,
    *,
    start_sample: int,
    end_sample: int,
    number: int,
) -> Track:
    first = side.project.tracks[0]
    title = side.project.metadata.get("album") or side.project.metadata.get("title")
    lease_title = title or first.album or "Album"
    return Track(
        number=number,
        title=f"{lease_title} - Side {side.planned.label}",
        start_sample=start_sample,
        end_sample=end_sample,
        start_seconds=start_sample / side.project.source.sample_rate,
        end_seconds=end_sample / side.project.source.sample_rate,
        artist=first.artist,
        album=first.album,
        album_artist=first.album_artist,
        year=first.year,
        genre=first.genre,
        side=side.planned.label,
    )


def _verification_payload(value: Any) -> dict[str, Any]:
    return {
        "codec_name": value.codec_name,
        "sample_rate": value.sample_rate,
        "channels": value.channels,
        "bits_per_raw_sample": value.bits_per_raw_sample,
        "exact_sample_count": value.exact_sample_count,
        "presentation_sample_count": value.presentation_sample_count,
        "decoded_pcm_sha256": value.decoded_pcm_sha256,
        "source_range_pcm_sha256": value.source_range_pcm_sha256,
        "complete_decode_verified": True,
    }


def _semantic_tags_and_artwork(path: Path) -> dict[str, Any]:
    completed = run_bounded_capture(
        [
            find_tool("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            ("format_tags:stream=index,codec_type:stream_disposition=attached_pic:stream_tags"),
            "-of",
            "json",
            str(path),
        ],
        stdout_limit=1024 * 1024,
    )
    if completed.returncode != 0 or completed.stdout_truncated or completed.stderr_truncated:
        raise ExportError(f"Audio metadata could not be verified for {path.name!r}.")
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
        streams = payload["streams"]
        format_value = payload.get("format", {})
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ExportError(f"FFprobe returned invalid metadata for {path.name!r}.") from exc
    if not isinstance(streams, list) or not isinstance(format_value, dict):
        raise ExportError(f"FFprobe returned invalid streams for {path.name!r}.")
    semantic: dict[str, str] = {}

    def consume(raw: Any) -> None:
        if raw is None:
            return
        if not isinstance(raw, dict):
            raise ExportError(f"Audio tags are invalid for {path.name!r}.")
        for raw_key, raw_value in raw.items():
            if not isinstance(raw_key, str) or not isinstance(raw_value, str):
                raise ExportError(f"Audio tags are invalid for {path.name!r}.")
            key = raw_key.casefold()
            if key not in _SEMANTIC_AUDIO_TAGS:
                continue
            previous = semantic.get(key)
            if previous is not None and previous != raw_value:
                raise ExportError(f"Audio tag {key!r} conflicts between container scopes.")
            semantic[key] = raw_value

    consume(format_value.get("tags"))
    attached_streams = 0
    for stream in streams:
        if not isinstance(stream, dict):
            raise ExportError(f"FFprobe returned an invalid stream for {path.name!r}.")
        codec_type = stream.get("codec_type")
        if codec_type not in {"audio", "video"}:
            raise ExportError(f"FFprobe returned an invalid stream type for {path.name!r}.")
        if codec_type == "audio":
            consume(stream.get("tags"))
        disposition = stream.get("disposition", {})
        if not isinstance(disposition, dict):
            raise ExportError(f"FFprobe returned invalid stream disposition for {path.name!r}.")
        attached = disposition.get("attached_pic", 0)
        if type(attached) is not int or attached not in {0, 1}:
            raise ExportError(f"FFprobe returned invalid attached-picture state for {path.name!r}.")
        attached_streams += attached
    if attached_streams > 1:
        raise ExportError(f"Audio file {path.name!r} has multiple attached pictures.")
    artwork_sha256: str | None = None
    if attached_streams == 1:
        artwork = run_bounded_capture(
            [
                find_tool("ffmpeg"),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-c:v",
                "copy",
                "-f",
                "image2pipe",
                "pipe:1",
            ],
            stdout_limit=_MAX_EMBEDDED_ARTWORK_BYTES,
        )
        if (
            artwork.returncode != 0
            or artwork.stdout_truncated
            or artwork.stderr_truncated
            or not artwork.stdout
        ):
            raise ExportError(f"Embedded artwork could not be verified for {path.name!r}.")
        artwork_sha256 = hashlib.sha256(artwork.stdout).hexdigest()
    return {
        "semantic_tags": {key: semantic[key] for key in sorted(semantic)},
        "attached_picture_count": attached_streams,
        "embedded_artwork_sha256": artwork_sha256,
    }


def _audio_attestation(path: Path) -> dict[str, Any]:
    details = _probe_exact_audio_stream(path)
    _complete_decode(path)
    codec = details["codec_name"]
    if codec == "flac":
        bits = details["bits_per_raw_sample"]
        if bits not in {16, 24}:
            raise ExportError(f"FLAC {path.name!r} has unsupported PCM precision.")
        pcm_format = "s32le" if bits > 16 else "s16le"
        presentation_samples: int | None = None
    elif codec == "aac" and path.suffix.casefold() == ".m4a":
        pcm_format = "s16le"
        presentation_samples = _probe_m4a_presentation_sample_count(
            path,
            int(details["sample_rate"]),
        )
    else:
        raise ExportError(f"Audio artifact {path.name!r} has an unsupported codec.")
    return {
        **details,
        "presentation_sample_count": presentation_samples,
        "decoded_pcm_sha256": _decoded_pcm_sha256(
            path,
            sample_format=pcm_format,
        ),
        "complete_decode_verified": True,
        **_semantic_tags_and_artwork(path),
    }


def _inventory_item(
    stage: Path,
    path: Path,
    *,
    profile: str,
    role: str,
    **details: Any,
) -> dict[str, Any]:
    receipt = capture_file_receipt(path, label=f"Staged {role}")
    normalized_details = dict(details)
    if path.suffix.casefold() in {".flac", ".m4a"}:
        raw_verification = normalized_details.get("verification", {})
        if not isinstance(raw_verification, dict):
            raise ExportError("Staged audio verification must be a JSON object.")
        normalized_details["verification"] = {
            **raw_verification,
            "audio_attestation": _audio_attestation(path),
        }
    return {
        "path": path.relative_to(stage).as_posix(),
        "profile": profile,
        "role": role,
        **_receipt_payload(receipt),
        **normalized_details,
    }


def _render_music_range(
    staged: _StagedSide,
    destination: Path,
    settings: PublicationSettings,
    metadata: Mapping[str, str],
) -> dict[str, Any]:
    side = staged.lease
    count = side.music_end - side.music_start
    track = _side_track(
        side,
        start_sample=side.music_start,
        end_sample=side.music_end,
        number=1,
    )
    verification = render_verified_track(
        source_snapshot=staged.source_snapshot,
        staged_path=destination,
        track=track,
        total_tracks=1,
        output_format="flac",
        expected_sample_count=count,
        source_sample_rate=side.project.source.sample_rate,
        source_channels=side.project.source.channels,
        source_bits=side.project.source.bits_per_raw_sample,
        flac_compression=settings.flac_compression,
        aac_bitrate=f"{settings.aac_bitrate_kbps}k",
        project_metadata=metadata,
    )
    return _verification_payload(verification)


def _materialize_profiles(
    lease: _ExecutionLease,
    staged_sides: tuple[_StagedSide, ...],
    artwork: Path | None,
    stage: Path,
    settings: PublicationSettings,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    archival_sources: dict[str, Any] = {
        "objects": [],
        "side_bindings": [],
    }
    metadata = _album_metadata(lease)
    selected = set(lease.plan.selected_profiles)
    total_tracks = sum(len(side.project.tracks) for side in lease.sides)

    if "archival-source" in selected:
        root = stage / "archival-source"
        root.mkdir()
        staged_by_label = {side.lease.planned.label: side for side in staged_sides}
        for source_object in _source_objects(lease.sides):
            first_side = source_object.sides[0]
            staged_source = staged_by_label[first_side.planned.label]
            destination = root / _source_object_name(source_object)
            receipt = stage_verified_copy(
                staged_source.source_snapshot,
                destination,
                staged_source.source_snapshot_receipt,
                label=f"Archival source object {source_object.object_id}",
            )
            relative = destination.relative_to(stage).as_posix()
            inventory.append(
                {
                    "path": relative,
                    "profile": "archival-source",
                    "role": "full-capture-source",
                    **_receipt_payload(receipt),
                    "source_object_id": source_object.object_id,
                    "first_side_order": source_object.first_side_order,
                    "verified_byte_identical": True,
                    **(
                        {"verification": {"audio_attestation": _audio_attestation(destination)}}
                        if destination.suffix.casefold() in {".flac", ".m4a"}
                        else {}
                    ),
                }
            )
            archival_sources["objects"].append(
                {
                    "object_id": source_object.object_id,
                    "path": relative,
                    "source_sha256": receipt.sha256,
                    "source_size_bytes": receipt.size_bytes,
                    "first_side_order": source_object.first_side_order,
                    "verified_byte_identical": True,
                }
            )
            for side in source_object.sides:
                archival_sources["side_bindings"].append(
                    {
                        "side_order": side.planned.order,
                        "side_label": side.planned.label,
                        "side_project_sha256": (side.planned.current_identity.project_sha256),
                        "source_object_id": source_object.object_id,
                        "source_sha256": side.source_receipt.sha256,
                        "source_size_bytes": side.source_receipt.size_bytes,
                    }
                )
        archival_sources["side_bindings"].sort(key=lambda item: int(item["side_order"]))

    if "restored-side" in selected:
        root = stage / "restored-side"
        root.mkdir()
        for staged_side in staged_sides:
            side = staged_side.lease
            if side.restoration is None:
                raise ExportError(
                    "The restored-side profile requires a reviewed outcome for every side."
                )
            destination = root / _side_name(side)
            if staged_side.restoration_audio_snapshot is not None:
                source_receipt = capture_file_receipt(
                    staged_side.restoration_audio_snapshot,
                    label=f"Side {side.planned.label} restored snapshot",
                )
                stage_verified_copy(
                    staged_side.restoration_audio_snapshot,
                    destination,
                    source_receipt,
                    label=f"Side {side.planned.label} restored output",
                )
                details = _probe_exact_audio_stream(destination)
                _complete_decode(destination)
                verification = {
                    **details,
                    "complete_decode_verified": True,
                    "validated_restoration_render": True,
                }
            else:
                verification = _render_music_range(
                    staged_side,
                    destination,
                    settings,
                    metadata,
                )
                verification["reviewed_clean_pcm_equal"] = True
            inventory.append(
                _inventory_item(
                    stage,
                    destination,
                    profile="restored-side",
                    role="music-range-side",
                    side_order=side.planned.order,
                    side_label=side.planned.label,
                    verification=verification,
                )
            )

    needs_lossless = bool({"corrected-lossless", "portable"} & selected)
    if not needs_lossless:
        shutil.rmtree(stage / ".work")
        return inventory, archival_sources
    work = stage / ".work"
    music_root = work / "music"
    corrected_root = work / "corrected"
    music_root.mkdir()
    corrected_root.mkdir()
    lossless_root = (
        stage / "corrected-lossless"
        if "corrected-lossless" in selected
        else work / "corrected-lossless"
    )
    lossless_root.mkdir()
    lossless_tracks: list[_LosslessTrack] = []
    album_offset = 0
    for staged_side in staged_sides:
        side = staged_side.lease
        music_path = music_root / _side_name(side)
        if staged_side.restoration_audio_snapshot is not None:
            receipt = capture_file_receipt(
                staged_side.restoration_audio_snapshot,
                label=f"Side {side.planned.label} restored music snapshot",
            )
            stage_verified_copy(
                staged_side.restoration_audio_snapshot,
                music_path,
                receipt,
                label=f"Side {side.planned.label} music material",
            )
        else:
            _render_music_range(staged_side, music_path, settings, metadata)

        music_count = side.music_end - side.music_start
        factor = side.speed.selected_effective_speed_factor
        asetrate_hz, effective_factor = speed_correction_details(
            side.project.source.sample_rate,
            factor,
        )
        correction_factor = None if math.isclose(factor, 1.0, abs_tol=1e-12) else factor
        corrected_count = (
            music_count
            if correction_factor is None
            else _speed_corrected_sample(
                music_count,
                side.project.source.sample_rate,
                asetrate_hz,
            )
        )
        corrected_path = corrected_root / _side_name(side)
        continuous = _side_track(
            side,
            start_sample=0,
            end_sample=music_count,
            number=album_offset + 1,
        )
        render_verified_track(
            source_snapshot=music_path,
            staged_path=corrected_path,
            track=continuous,
            total_tracks=total_tracks,
            output_format="flac",
            expected_sample_count=corrected_count,
            source_sample_rate=side.project.source.sample_rate,
            source_channels=side.project.source.channels,
            source_bits=side.project.source.bits_per_raw_sample,
            flac_compression=settings.flac_compression,
            aac_bitrate=f"{settings.aac_bitrate_kbps}k",
            artwork_path=artwork,
            project_metadata=metadata,
            source_speed_factor=correction_factor,
        )
        previous_end = 0
        for local_number, source_track in enumerate(side.project.tracks, start=1):
            relative_start = source_track.start_sample - side.music_start
            relative_end = source_track.end_sample - side.music_start
            mapped_start = (
                relative_start
                if correction_factor is None
                else _speed_corrected_sample(
                    relative_start,
                    side.project.source.sample_rate,
                    asetrate_hz,
                )
            )
            mapped_end = (
                relative_end
                if correction_factor is None
                else _speed_corrected_sample(
                    relative_end,
                    side.project.source.sample_rate,
                    asetrate_hz,
                )
            )
            if mapped_start != previous_end or mapped_end <= mapped_start:
                raise ExportError(
                    f"Side {side.planned.label} corrected boundaries are not adjacent."
                )
            previous_end = mapped_end
            album_number = album_offset + local_number
            track = Track.from_dict(asdict(source_track))
            track.number = album_number
            track.start_sample = mapped_start
            track.end_sample = mapped_end
            track.start_seconds = mapped_start / side.project.source.sample_rate
            track.end_seconds = mapped_end / side.project.source.sample_rate
            track.side = side.planned.label
            for field_name, value in (
                ("artist", metadata.get("artist", "")),
                ("album", metadata.get("album") or metadata.get("title", "")),
                ("album_artist", metadata.get("album_artist", "")),
                ("year", metadata.get("year", "")),
                ("genre", metadata.get("genre", "")),
            ):
                if value:
                    setattr(track, field_name, value)
            prefix = f"{album_number:02d}-"
            title = sanitize_filename(
                track.title,
                f"Track {album_number:02d}",
                prefix=prefix,
                suffix=".flac",
            )
            destination = lossless_root / f"{prefix}{title}.flac"
            track_verification = render_verified_track(
                source_snapshot=corrected_path,
                staged_path=destination,
                track=track,
                total_tracks=total_tracks,
                output_format="flac",
                expected_sample_count=mapped_end - mapped_start,
                source_sample_rate=side.project.source.sample_rate,
                source_channels=side.project.source.channels,
                source_bits=side.project.source.bits_per_raw_sample,
                flac_compression=settings.flac_compression,
                aac_bitrate=f"{settings.aac_bitrate_kbps}k",
                artwork_path=artwork,
                project_metadata=metadata,
            )
            lossless_tracks.append(
                _LosslessTrack(
                    side_label=side.planned.label,
                    album_track_number=album_number,
                    track=track,
                    path=destination,
                    sample_count=mapped_end - mapped_start,
                )
            )
            if "corrected-lossless" in selected:
                inventory.append(
                    _inventory_item(
                        stage,
                        destination,
                        profile="corrected-lossless",
                        role="corrected-track",
                        side_order=side.planned.order,
                        side_label=side.planned.label,
                        local_track_number=local_number,
                        album_track_number=album_number,
                        source_start_sample=source_track.start_sample,
                        source_end_sample=source_track.end_sample,
                        relative_source_start_sample=relative_start,
                        relative_source_end_sample=relative_end,
                        corrected_start_sample=mapped_start,
                        corrected_end_sample=mapped_end,
                        requested_speed_factor=factor,
                        effective_speed_factor=effective_factor,
                        asetrate_hz=asetrate_hz,
                        verification=_verification_payload(track_verification),
                    )
                )
        if previous_end != corrected_count:
            raise ExportError(
                f"Side {side.planned.label} final track does not reach the music end."
            )
        album_offset += len(side.project.tracks)

    if "portable" in selected:
        portable_root = stage / "portable"
        portable_root.mkdir()
        for item in lossless_tracks:
            destination = portable_root / f"{item.path.stem}.m4a"
            portable_track = Track.from_dict(asdict(item.track))
            portable_track.start_sample = 0
            portable_track.end_sample = item.sample_count
            portable_track.start_seconds = 0.0
            portable_track.end_seconds = item.sample_count / next(
                side.project.source.sample_rate
                for side in lease.sides
                if side.planned.label == item.side_label
            )
            side = next(side for side in lease.sides if side.planned.label == item.side_label)
            portable_verification = render_verified_track(
                source_snapshot=item.path,
                staged_path=destination,
                track=portable_track,
                total_tracks=total_tracks,
                output_format="m4a",
                expected_sample_count=item.sample_count,
                source_sample_rate=side.project.source.sample_rate,
                source_channels=side.project.source.channels,
                source_bits=side.project.source.bits_per_raw_sample,
                flac_compression=settings.flac_compression,
                aac_bitrate=f"{settings.aac_bitrate_kbps}k",
                artwork_path=artwork,
                project_metadata=metadata,
            )
            inventory.append(
                _inventory_item(
                    stage,
                    destination,
                    profile="portable",
                    role="portable-track",
                    side_label=item.side_label,
                    album_track_number=item.album_track_number,
                    encoded_from="staged-corrected-lossless-flac",
                    lossless_input_sha256=capture_file_receipt(
                        item.path,
                        label="Portable lossless input",
                    ).sha256,
                    verification=_verification_payload(portable_verification),
                )
            )
    shutil.rmtree(work)
    return inventory, archival_sources


def _walk_regular_files(root: Path) -> set[str]:
    files: set[str] = set()
    pending = [(root, 0)]
    entry_count = 0
    while pending:
        directory, depth = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError as exc:
            raise ExportError("The publication stage could not be enumerated.") from exc
        with entries:
            for entry in entries:
                entry_count += 1
                if entry_count > _MAX_STAGE_ENTRIES:
                    raise ExportError("The publication stage contains too many entries.")
                path = Path(entry.path)
                try:
                    metadata = path.lstat()
                except OSError as exc:
                    raise ExportError("A staged entry could not be inspected.") from exc
                attributes = int(getattr(metadata, "st_file_attributes", 0))
                if stat.S_ISLNK(metadata.st_mode) or attributes & _REPARSE_POINT:
                    raise ExportError("Publication stages cannot contain reparse points.")
                if stat.S_ISDIR(metadata.st_mode):
                    if depth >= _MAX_STAGE_DEPTH:
                        raise ExportError("The publication stage is nested too deeply.")
                    pending.append((path, depth + 1))
                elif stat.S_ISREG(metadata.st_mode):
                    files.add(path.relative_to(root).as_posix())
                else:
                    raise ExportError("Publication stages may contain only regular files.")
    return files


def _append_provenance_inventory(
    stage: Path,
    inventory: list[dict[str, Any]],
) -> None:
    for path in sorted((stage / "provenance").rglob("*")):
        if path.is_file():
            inventory.append(
                _inventory_item(
                    stage,
                    path,
                    profile="provenance",
                    role="input-snapshot",
                )
            )
    artwork = stage / "artwork"
    if artwork.is_dir():
        for path in sorted(artwork.iterdir()):
            inventory.append(
                _inventory_item(
                    stage,
                    path,
                    profile="artwork",
                    role="album-artwork",
                )
            )


def _manifest(
    lease: _ExecutionLease,
    inventory: list[dict[str, Any]],
    archival_sources: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": ALBUM_PUBLICATION_MANIFEST_SCHEMA,
        "plan": {
            "raw_file_sha256": lease.raw_plan_sha256,
            "body_sha256": lease.plan.body_sha256,
            "plan_sha256": lease.plan.plan_sha256,
            "sibling_filename": lease.plan_path.name,
        },
        "album": {
            **_receipt_payload(lease.album_receipt),
            "sibling_filename": lease.album_path.name,
        },
        "selected_profiles": list(lease.plan.selected_profiles),
        "restoration_mode": lease.restoration_mode,
        "tools": asdict(lease.observations),
        "processing_nodes": [node.to_dict() for node in lease.plan.nodes],
        "archival_sources": dict(archival_sources),
        "sides": [
            {
                "order": side.planned.order,
                "label": side.planned.label,
                "identity": side.identity.to_dict(),
                "speed": side.speed.to_dict(),
                "music_start_sample": side.music_start,
                "music_end_sample_exclusive": side.music_end,
                "restoration": (
                    {
                        "outcome": side.restoration.kind,
                        "manifest_sha256": side.restoration.manifest_receipt.sha256,
                        "audio_sha256": (
                            side.restoration.audio_receipt.sha256
                            if side.restoration.audio_receipt is not None
                            else None
                        ),
                    }
                    if side.restoration is not None
                    else None
                ),
            }
            for side in lease.sides
        ],
        "inventory": sorted(inventory, key=lambda item: str(item["path"])),
    }


def _verify_stage_tree(stage: Path, inventory: list[dict[str, Any]]) -> None:
    expected = {str(item["path"]) for item in inventory}
    if len(expected) != len(inventory):
        raise ExportError("The publication inventory contains duplicate paths.")
    actual = _walk_regular_files(stage)
    allowed = expected | {_MANIFEST_NAME, _JOURNAL_NAME}
    if actual != allowed:
        unexpected = sorted(actual - allowed)
        missing = sorted(allowed - actual)
        raise ExportError(
            "The staged publication tree differs from its inventory "
            f"(unexpected={unexpected}, missing={missing})."
        )
    for item in inventory:
        relative = str(item["path"])
        receipt = capture_file_receipt(
            stage / Path(relative),
            label=f"Staged artifact {relative}",
        )
        if receipt.sha256 != item.get("sha256") or receipt.size_bytes != item.get("size_bytes"):
            raise ExportError(f"Staged artifact {relative!r} changed after inventory.")


def _assert_live_lease(
    lease: _ExecutionLease,
    settings: PublicationSettings,
) -> None:
    assert_file_receipt(
        lease.plan_path,
        lease.plan_receipt,
        label="Album publication plan",
    )
    assert_file_receipt(lease.album_path, lease.album_receipt, label="Album project")
    for side in lease.sides:
        assert_file_receipt(
            side.project_path,
            side.project_receipt,
            label=f"Side {side.planned.label} project",
        )
        assert_file_receipt(
            side.source_path,
            side.source_receipt,
            label=f"Side {side.planned.label} source",
        )
        if side.restoration is not None:
            assert_file_receipt(
                side.restoration.manifest_path,
                side.restoration.manifest_receipt,
                label=f"Side {side.planned.label} restoration manifest",
            )
            if (
                side.restoration.audio_path is not None
                and side.restoration.audio_receipt is not None
            ):
                assert_file_receipt(
                    side.restoration.audio_path,
                    side.restoration.audio_receipt,
                    label=f"Side {side.planned.label} restoration audio",
                )
            workspace = (
                side.restoration.manifest_path.parent.parent
                if side.restoration.kind == "render"
                else side.restoration.manifest_path.parent
            )
            current_catalog = discover_restoration_catalog(
                workspace,
                side.project_path,
                verified_source_sha256=side.source_receipt.sha256,
            )
            if current_catalog != side.restoration.catalog:
                raise ExportError(
                    f"Side {side.planned.label} restoration catalog changed during publication."
                )
            selection = current_catalog.latest_chain()
            selected_artifact = (
                selection.render if side.restoration.kind == "render" else selection.scan
            )
            if (
                selected_artifact is None
                or selected_artifact.artifact_id != side.restoration.artifact.artifact_id
            ):
                raise ExportError(
                    f"Side {side.planned.label} restoration outcome is no "
                    "longer the latest current chain."
                )
    if lease.artwork_path is not None and lease.artwork_receipt is not None:
        assert_file_receipt(
            lease.artwork_path,
            lease.artwork_receipt,
            label="Album artwork",
        )
    current_tools = observe_publication_tools()
    if current_tools != lease.observations:
        raise ExportError("Publication tools changed during execution.")
    by_label = {side.planned.label: side for side in lease.sides}
    for node in lease.plan.nodes:
        _validate_node_binding(
            node,
            side_by_label=by_label,
            settings=settings,
            observations=current_tools,
            restoration_mode=lease.restoration_mode,
        )


def _estimate_storage(lease: _ExecutionLease) -> int:
    selected = set(lease.plan.selected_profiles)
    total = lease.plan_receipt.size_bytes + lease.album_receipt.size_bytes
    source_objects = _source_objects(lease.sides)
    total += sum(item.source_receipt.size_bytes for item in source_objects)
    if "archival-source" in selected:
        total += sum(item.source_receipt.size_bytes for item in source_objects)
    if lease.artwork_receipt is not None:
        total += lease.artwork_receipt.size_bytes * 3
    for side in lease.sides:
        project = side.project
        bits = project.source.bits_per_raw_sample or 24
        bytes_per_frame = project.source.channels * max(2, (bits + 7) // 8)
        music_frames = side.music_end - side.music_start
        _asetrate, effective = speed_correction_details(
            project.source.sample_rate,
            side.speed.selected_effective_speed_factor,
        )
        corrected_frames = math.ceil(music_frames * effective)
        total += side.project_receipt.size_bytes
        if side.restoration is not None:
            total += side.restoration.manifest_receipt.size_bytes
            if side.restoration.audio_receipt is not None:
                total += side.restoration.audio_receipt.size_bytes * 3
        if "restored-side" in selected:
            total += music_frames * bytes_per_frame
        if {"corrected-lossless", "portable"} & selected:
            total += (music_frames + 2 * corrected_frames) * bytes_per_frame
        if "portable" in selected:
            total += corrected_frames * bytes_per_frame
    return total + _FILE_OVERHEAD_BYTES * (16 + len(lease.sides) * 8)


def _remove_owned_stage(
    stage: Path,
    expected_identity: _DirectoryIdentity,
) -> None:
    files: list[Path] = []
    directories: list[tuple[Path, int]] = []
    pending = [(stage, 0)]
    entry_count = 0
    while pending:
        directory, depth = pending.pop()
        directories.append((directory, depth))
        try:
            entries = os.scandir(directory)
        except OSError as exc:
            raise ExportError("The owned publication stage could not be read.") from exc
        with entries:
            for entry in entries:
                entry_count += 1
                if entry_count > _MAX_STAGE_ENTRIES:
                    raise ExportError("The owned publication stage has too many entries.")
                path = Path(entry.path)
                try:
                    metadata = path.lstat()
                except OSError as exc:
                    raise ExportError("An owned staged entry could not be inspected.") from exc
                attributes = int(getattr(metadata, "st_file_attributes", 0))
                if stat.S_ISLNK(metadata.st_mode) or attributes & _REPARSE_POINT:
                    raise ExportError(
                        "Refusing to clean a publication stage containing a reparse point."
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    if depth >= _MAX_STAGE_DEPTH:
                        raise ExportError("The owned publication stage is nested too deeply.")
                    pending.append((path, depth + 1))
                elif stat.S_ISREG(metadata.st_mode):
                    files.append(path)
                else:
                    raise ExportError(
                        "Refusing to clean a publication stage with an unsafe file type."
                    )
    if _directory_identity(stage, label="Owned publication stage") != expected_identity:
        raise ExportError("The publication stage was substituted before cleanup.")
    for path in files:
        try:
            metadata = path.lstat()
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if not stat.S_ISREG(metadata.st_mode) or attributes & _REPARSE_POINT:
                raise ExportError("A staged file was substituted before safe cleanup.")
            path.unlink()
        except ExportError:
            raise
        except OSError as exc:
            raise ExportError("An owned staged file could not be removed.") from exc
    for directory, _depth in sorted(
        directories,
        key=lambda item: item[1],
        reverse=True,
    ):
        if directory == stage:
            if _directory_identity(directory, label="Owned publication stage") != expected_identity:
                raise ExportError("The publication stage was substituted during cleanup.")
        else:
            _directory_identity(directory, label="Owned staged directory")
        try:
            directory.rmdir()
        except OSError as exc:
            raise ExportError("An owned staged directory could not be removed.") from exc


def _cleanup_stage(
    stage: Path,
    expected_parent: Path,
    expected_identity: _DirectoryIdentity,
) -> None:
    if stage.parent != expected_parent or not (
        stage.name.startswith(_STAGE_PREFIX) and stage.name.endswith(_STAGE_SUFFIX)
    ):
        raise ExportError(f"Refusing to remove unexpected publication stage: {stage}")
    if not os.path.lexists(stage):
        return
    if _directory_identity(stage, label="Publication stage") != expected_identity:
        raise ExportError("The publication stage path was substituted; it was not removed.")
    quarantine = expected_parent / (f".groove-serpent-album-cleanup-{uuid.uuid4().hex}.partial")
    _atomic_no_replace_directory(stage, quarantine)
    if _directory_identity(quarantine, label="Quarantined publication stage") != expected_identity:
        raise ExportError(
            "The publication stage changed during cleanup quarantine; it was not removed."
        )
    _remove_owned_stage(quarantine, expected_identity)


def preflight_album_publication_plan(
    plan_path: Path,
    *,
    settings: PublicationSettings | None = None,
) -> AlbumPublicationPreflightReport:
    """Revalidate an immutable plan and all live inputs without creating output."""

    lease = _capture_execution_lease(plan_path, settings)
    _assert_live_lease(lease, lease.settings)
    return AlbumPublicationPreflightReport(
        plan_sha256=lease.plan.plan_sha256,
        album_sha256=lease.album_receipt.sha256,
        selected_profiles=lease.plan.selected_profiles,
        side_count=len(lease.sides),
    )


def execute_album_publication_plan(
    plan_path: Path,
    output_directory: Path,
    *,
    settings: PublicationSettings | None = None,
    progress: Callable[[str], None] | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> AlbumPublicationExecutionReport:
    """Execute one immutable plan and atomically publish one directory tree."""

    _notify(progress, "Validating publication plan and current identities...")
    lease = _capture_execution_lease(plan_path, settings)
    output, exists = _resolve_portable_export_path(
        output_directory,
        context="album publication directory",
        create_parents=True,
    )
    if exists or portable_path_entry_exists(output):
        raise ExportError(f"Publication output already exists: {output}")
    ensure_free_space(
        output.parent,
        _estimate_storage(lease),
        label="Album publication",
    )
    operation_id = uuid.uuid4().hex
    stage = output.parent / f"{_STAGE_PREFIX}{operation_id}{_STAGE_SUFFIX}"
    if portable_path_entry_exists(stage):
        raise ExportError("A private publication stage unexpectedly already exists.")
    stage.mkdir()
    stage_identity = _directory_identity(stage, label="Publication stage")
    try:
        _inject_fault(fault_injector, "after-stage-created")
        _journal(
            stage,
            "staging",
            lease.plan.plan_sha256,
            operation_id=operation_id,
            intended_output_name=output.name,
            stage_identity=stage_identity,
        )
        _inject_fault(fault_injector, "after-journal-staging")
        _notify(progress, "Creating immutable input snapshots...")
        staged_sides, artwork = _stage_snapshots(lease, stage)
        _notify(progress, "Rendering selected publication profiles...")
        inventory, archival_sources = _materialize_profiles(
            lease,
            staged_sides,
            artwork,
            stage,
            lease.settings,
        )
        _notify(progress, "Writing exact sample chapters and approximate CUE navigation...")
        _write_navigation_artifacts(
            lease,
            stage,
            inventory,
            archival_sources,
        )
        _append_provenance_inventory(stage, inventory)
        manifest = _manifest(lease, inventory, archival_sources)
        _write_json(stage / _MANIFEST_NAME, manifest)
        _verify_stage_tree(stage, inventory)
        _inject_fault(fault_injector, "after-manifest-verified")
        _journal(
            stage,
            "verified-ready",
            lease.plan.plan_sha256,
            operation_id=operation_id,
            intended_output_name=output.name,
            stage_identity=stage_identity,
        )
        _inject_fault(fault_injector, "after-journal-ready")
        _verify_stage_tree(stage, inventory)
        if _directory_identity(stage, label="Publication stage") != stage_identity:
            raise ExportError("The publication stage was substituted before commit.")
        _notify(progress, "Revalidating live inputs before atomic commit...")
        _assert_live_lease(lease, lease.settings)
        _verify_stage_tree(stage, inventory)
        if _directory_identity(stage, label="Publication stage") != stage_identity:
            raise ExportError(
                "The publication stage was substituted during live-input revalidation."
            )
        _resolved, now_exists = _resolve_portable_export_path(
            output,
            context="album publication directory",
        )
        if now_exists or portable_path_entry_exists(output):
            raise ExportError("Publication output appeared before atomic commit.")
        if _directory_identity(stage, label="Publication stage") != stage_identity:
            raise ExportError("The publication stage was substituted before commit.")
        _inject_fault(fault_injector, "before-commit")
        _atomic_no_replace_directory(stage, output)
    except BaseException as exc:
        try:
            _cleanup_stage(stage, output.parent, stage_identity)
        except BaseException:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise exc
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, ExportError):
            raise
        if isinstance(exc, GrooveSerpentError):
            raise ExportError(str(exc)) from exc
        raise ExportError(f"Album publication failed: {exc}") from exc

    _inject_fault(fault_injector, "after-commit")
    artifacts = tuple(
        PublishedArtifact(
            profile=str(item["profile"]),
            role=str(item["role"]),
            relative_path=str(item["path"]),
            size_bytes=int(item["size_bytes"]),
            sha256=str(item["sha256"]),
        )
        for item in sorted(inventory, key=lambda value: str(value["path"]))
    )
    _notify(progress, f"Published {len(artifacts)} verified artifacts.")
    return AlbumPublicationExecutionReport(
        output_directory=str(output),
        manifest_path=str(output / _MANIFEST_NAME),
        plan_sha256=lease.plan.plan_sha256,
        artifacts=artifacts,
    )


__all__ = [
    "ALBUM_PUBLICATION_JOURNAL_SCHEMA",
    "ALBUM_PUBLICATION_MANIFEST_SCHEMA",
    "LEGACY_ALBUM_PUBLICATION_MANIFEST_SCHEMA",
    "AlbumPublicationExecutionReport",
    "AlbumPublicationPreflightReport",
    "PublishedArtifact",
    "execute_album_publication_plan",
    "preflight_album_publication_plan",
]
