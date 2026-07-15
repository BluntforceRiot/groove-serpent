"""Build one immutable, coherent production album publication plan."""

from __future__ import annotations

import os
import stat
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .album import (
    AlbumSide,
    load_album_project_with_sha256,
    project_speed_state,
    resolve_album_reference,
)
from .album_publication_plan import (
    PROFILE_ARCHIVAL_SOURCE,
    PROFILE_CORRECTED_LOSSLESS,
    PROFILE_PORTABLE,
    PROFILE_RESTORED_SIDE,
    RESTORATION_NO_DERIVATIVE_SCHEMA,
    RESTORATION_RENDER_SCHEMA,
    RESTORATION_SCAN_SCHEMA,
    AlbumPublicationPlan,
    ProcessingInput,
    ProcessingNode,
    ProfileOutput,
    PublicationSide,
    RestorationNoDerivativeBinding,
    RestorationRenderBinding,
    SideIdentity,
    save_album_publication_plan,
)
from .album_publication_policy import (
    PublicationSettings,
    ToolObservations,
    observe_publication_tools,
    operation_tool_binding,
)
from .errors import ExportError, ProjectValidationError
from .models import Project, resolve_source_path
from .project_io import load_project_with_sha256
from .publication import FileReceipt, assert_file_receipt, capture_file_receipt
from .restoration_catalog import (
    RestorationArtifact,
    RestorationCatalog,
    discover_restoration_catalog,
)


_PROFILE_ORDER = (
    PROFILE_ARCHIVAL_SOURCE,
    PROFILE_RESTORED_SIDE,
    PROFILE_CORRECTED_LOSSLESS,
    PROFILE_PORTABLE,
)
_RESTORATION_AWARE_PROFILES = {
    PROFILE_RESTORED_SIDE,
    PROFILE_CORRECTED_LOSSLESS,
    PROFILE_PORTABLE,
}
_WORKSPACE_DIRECTORY = ".groove-serpent"
_MAX_DESTINATION_SIBLINGS = 100_000
_WINDOWS_DEVICE_STEMS = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class _CapturedFile:
    path: Path
    receipt: FileReceipt
    label: str


@dataclass(frozen=True, slots=True)
class _CatalogCapture:
    workspace: Path
    project_path: Path
    source_sha256: str
    catalog: RestorationCatalog
    side_label: str


@dataclass(frozen=True, slots=True)
class _CapturedSide:
    album_side: AlbumSide
    project_path: Path
    project: Project
    identity: SideIdentity
    publication_side: PublicationSide


def default_restoration_workspace(project_path: Path | str) -> Path:
    """Return ReviewServer's deterministic, contained restoration workspace."""

    project = Path(project_path).expanduser().resolve()
    project_root = project.parent.resolve()
    safe_stem = (
        "-".join(
            part
            for part in "".join(
                character if character.isascii() and character.isalnum() else "-"
                for character in project.stem
            ).split("-")
            if part
        )[:80]
        or "project"
    )
    workspace = (
        project_root / _WORKSPACE_DIRECTORY / "restoration" / safe_stem
    ).absolute()
    try:
        workspace.relative_to(project_root)
    except ValueError as exc:
        raise ProjectValidationError(
            "The restoration workspace must remain inside the side-project folder."
        ) from exc
    if workspace.resolve() != workspace:
        raise ProjectValidationError(
            "The restoration workspace may not traverse a symlink or reparse point."
        )
    return workspace


def _capture(path: Path, *, label: str) -> _CapturedFile:
    try:
        receipt = capture_file_receipt(path, label=label)
    except ExportError as exc:
        raise ProjectValidationError(f"{label} could not be captured coherently.") from exc
    return _CapturedFile(path, receipt, label)


def _assert_captured(item: _CapturedFile) -> None:
    try:
        assert_file_receipt(item.path, item.receipt, label=item.label)
    except ExportError as exc:
        raise ProjectValidationError(
            f"{item.label} changed while the publication plan was being built."
        ) from exc


def _portable_relative(path: Path, root: Path, *, label: str) -> str:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ProjectValidationError(
            f"{label} must remain inside the album-project folder."
        ) from exc
    if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ProjectValidationError(f"{label} is not a safe portable relative path.")
    return relative


def _portable_name_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _assert_destination_available(destination: Path) -> None:
    target_key = _portable_name_key(destination.name)
    try:
        count = 0
        with os.scandir(destination.parent) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_DESTINATION_SIBLINGS:
                    raise ProjectValidationError(
                        "Publication-plan folder contains too many sibling entries "
                        "to prove a portable-unique destination."
                    )
                if _portable_name_key(entry.name) == target_key:
                    raise ProjectValidationError(
                        "Publication plan or a portable-equivalent sibling already "
                        f"exists: {entry.name}."
                    )
    except ProjectValidationError:
        raise
    except OSError as exc:
        raise ProjectValidationError(
            "Publication-plan folder could not be checked for portable collisions."
        ) from exc


def _validate_destination_name(name: str) -> None:
    if (
        not name
        or len(name) > 255
        or name != name.strip()
        or unicodedata.normalize("NFC", name) != name
        or any(ord(character) < 32 for character in name)
        or any(character in '<>:"/\\|?*' for character in name)
        or name.endswith((" ", "."))
        or name.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_STEMS
        or Path(name).suffix.casefold() != ".json"
    ):
        raise ProjectValidationError(
            "Publication-plan filename must be canonical portable JSON text and "
            "must not use a reserved Windows device name."
        )


def _validated_album_path(album_path: Path) -> Path:
    supplied = album_path.expanduser()
    absolute = Path(os.path.abspath(os.fspath(supplied)))
    try:
        parent = absolute.parent.resolve(strict=True)
        path = parent / absolute.name
        details = path.lstat()
    except OSError as exc:
        raise ProjectValidationError(
            "Album-project path could not be inspected safely."
        ) from exc
    attributes = getattr(details, "st_file_attributes", 0)
    reparse = isinstance(attributes, int) and bool(attributes & 0x0400)
    if stat.S_ISLNK(details.st_mode) or reparse or not stat.S_ISREG(details.st_mode):
        raise ProjectValidationError(
            "Album-project path must be a regular, non-reparse file."
        )
    return path


def _validated_destination(album_path: Path, plan_path: Path) -> Path:
    supplied = plan_path.expanduser()
    if any(part == ".." for part in supplied.parts):
        raise ProjectValidationError("Publication-plan destination may not contain '..'.")
    _validate_destination_name(supplied.name)
    absolute = Path(os.path.abspath(os.fspath(supplied)))
    try:
        parent = absolute.parent.resolve(strict=True)
    except OSError as exc:
        raise ProjectValidationError(
            "Publication-plan parent folder could not be resolved safely."
        ) from exc
    destination = parent / absolute.name
    if parent != album_path.parent:
        raise ProjectValidationError(
            "Publication plan must be placed directly beside its album project."
        )
    if destination == album_path:
        raise ProjectValidationError("Publication plan cannot replace the album project.")
    name = destination.name
    _validate_destination_name(name)
    _assert_destination_available(destination)
    return destination


def _selected_profiles(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ProjectValidationError(
            "Publication profiles must be supplied as a bounded collection."
        )
    requested_items: list[str] = []
    for item in values:
        requested_items.append(item)
        if len(requested_items) > len(_PROFILE_ORDER):
            raise ProjectValidationError(
                f"No more than {len(_PROFILE_ORDER)} publication profiles may be "
                "selected."
            )
    requested = tuple(requested_items)
    if not requested:
        raise ProjectValidationError("Select at least one publication profile.")
    if any(not isinstance(item, str) for item in requested):
        raise ProjectValidationError("Publication profile names must be text.")
    if len(set(requested)) != len(requested):
        raise ProjectValidationError("Publication profiles may be selected only once.")
    unsupported = set(requested) - set(_PROFILE_ORDER)
    if unsupported:
        rendered = ", ".join(repr(item) for item in sorted(unsupported))
        raise ProjectValidationError(f"Unsupported publication profile(s): {rendered}.")
    expanded = set(requested)
    if PROFILE_PORTABLE in expanded:
        expanded.add(PROFILE_CORRECTED_LOSSLESS)
    return tuple(profile for profile in _PROFILE_ORDER if profile in expanded)


def _validate_restoration_mode(
    profiles: tuple[str, ...], restoration_mode: str
) -> bool:
    if restoration_mode not in {"none", "reviewed"}:
        raise ProjectValidationError(
            "Restoration mode must be exactly 'none' or 'reviewed'."
        )
    restoration_aware = bool(set(profiles) & _RESTORATION_AWARE_PROFILES)
    if PROFILE_RESTORED_SIDE in profiles and restoration_mode != "reviewed":
        raise ProjectValidationError(
            "The restored-side profile requires restoration_mode='reviewed'."
        )
    if not restoration_aware and restoration_mode != "none":
        raise ProjectValidationError(
            "Archival-only publication does not consume reviewed restoration outcomes."
        )
    return restoration_aware and restoration_mode == "reviewed"


def _approved_side_identity(
    side: AlbumSide,
    project: Project,
    project_sha256: str,
    source_sha256: str,
) -> SideIdentity:
    speed_state = project_speed_state(project)
    current = SideIdentity(
        project_revision=project.revision,
        project_sha256=project_sha256,
        editable_state_sha256=project.state_sha256,
        source_sha256=source_sha256,
        project_speed_state_sha256=speed_state.sha256,
    )
    current.validate()
    if side.pin is None:
        raise ProjectValidationError(
            f"Side {side.label!r} is not approved and pinned for publication."
        )
    comparisons = (
        ("project revision", side.pin.project_revision, current.project_revision),
        ("project SHA-256", side.pin.project_sha256, current.project_sha256),
        (
            "editable-state SHA-256",
            side.pin.editable_state_sha256,
            current.editable_state_sha256,
        ),
        ("source SHA-256", side.pin.source_sha256, current.source_sha256),
        (
            "project speed-state SHA-256",
            side.pin.project_speed_state_sha256,
            current.project_speed_state_sha256,
        ),
        (
            "selected album speed-state SHA-256",
            side.pin.speed_state_sha256,
            side.speed.state_sha256,
        ),
    )
    mismatches = [label for label, expected, actual in comparisons if expected != actual]
    if mismatches:
        raise ProjectValidationError(
            f"Side {side.label!r} pin is stale: {', '.join(mismatches)}."
        )
    return current


def _artifact_file(artifact: RestorationArtifact, role: str) -> Path:
    matches = [item.path for item in artifact.files if item.role == role]
    if len(matches) != 1:
        raise ProjectValidationError(
            f"Restoration {artifact.kind} must bind exactly one {role!r} file."
        )
    return matches[0]


def _restoration_outcome(
    *,
    side_label: str,
    project_path: Path,
    project_sha256: str,
    source_sha256: str,
    plan_root: Path,
    captured_files: list[_CapturedFile],
    captured_catalogs: list[_CatalogCapture],
) -> tuple[
    RestorationRenderBinding | None,
    RestorationNoDerivativeBinding | None,
]:
    workspace = default_restoration_workspace(project_path)
    catalog = discover_restoration_catalog(
        workspace,
        project_path,
        verified_source_sha256=source_sha256,
    )
    if (
        catalog.project_sha256 != project_sha256
        or catalog.source_sha256 != source_sha256
    ):
        raise ProjectValidationError(
            f"Side {side_label!r} restoration catalog does not match current inputs."
        )
    captured_catalogs.append(
        _CatalogCapture(workspace, project_path, source_sha256, catalog, side_label)
    )
    selection = catalog.latest_chain()
    if selection.render is not None:
        render = selection.render
        restored_path = _artifact_file(render, "restored")
        manifest_capture = _capture(
            render.manifest_path,
            label=f"Side {side_label} restoration render manifest",
        )
        audio_capture = _capture(
            restored_path,
            label=f"Side {side_label} restored audio",
        )
        if manifest_capture.receipt.sha256 != render.manifest_sha256:
            raise ProjectValidationError(
                f"Side {side_label!r} render manifest changed after catalog validation."
            )
        restored_file = next(item for item in render.files if item.role == "restored")
        if audio_capture.receipt.sha256 != restored_file.sha256:
            raise ProjectValidationError(
                f"Side {side_label!r} restored audio changed after catalog validation."
            )
        captured_files.extend((manifest_capture, audio_capture))
        return (
            RestorationRenderBinding(
                schema=RESTORATION_RENDER_SCHEMA,
                manifest_reference=_portable_relative(
                    render.manifest_path,
                    plan_root,
                    label=f"Side {side_label} render manifest",
                ),
                manifest_sha256=render.manifest_sha256,
                audio_reference=_portable_relative(
                    restored_path,
                    plan_root,
                    label=f"Side {side_label} restored audio",
                ),
                audio_sha256=restored_file.sha256,
                project_sha256=project_sha256,
                source_sha256=source_sha256,
            ),
            None,
        )

    scan = selection.scan
    if scan is None:
        diagnostic = (
            f" ({len(catalog.invalid)} invalid, {len(catalog.stale)} stale artifact(s))"
            if catalog.invalid or catalog.stale
            else ""
        )
        raise ProjectValidationError(
            f"Side {side_label!r} has no current reviewed restoration outcome{diagnostic}."
        )
    payload = scan.payload
    coverage = payload.get("coverage")
    summary = payload.get("summary")
    candidates = payload.get("candidates")
    clean = (
        isinstance(coverage, dict)
        and coverage.get("restoration_status") == "complete"
        and coverage.get("scan_range_covers_music") is True
        and coverage.get("candidate_scan_truncated") is False
        and coverage.get("retained_candidates") == 0
        and isinstance(summary, dict)
        and summary.get("retained") == 0
        and summary.get("repairable") == 0
        and summary.get("truncated") is False
        and isinstance(candidates, list)
        and not candidates
    )
    if not clean:
        raise ProjectValidationError(
            f"Side {side_label!r} latest restoration scan is not a complete, "
            "untruncated zero-candidate outcome and has no validated render."
        )
    scan_capture = _capture(
        scan.manifest_path,
        label=f"Side {side_label} zero-candidate restoration scan",
    )
    if scan_capture.receipt.sha256 != scan.manifest_sha256:
        raise ProjectValidationError(
            f"Side {side_label!r} scan changed after catalog validation."
        )
    captured_files.append(scan_capture)
    return (
        None,
        RestorationNoDerivativeBinding(
            schema=RESTORATION_NO_DERIVATIVE_SCHEMA,
            scan_schema=RESTORATION_SCAN_SCHEMA,
            scan_reference=_portable_relative(
                scan.manifest_path,
                plan_root,
                label=f"Side {side_label} zero-candidate scan",
            ),
            scan_sha256=scan.manifest_sha256,
            project_sha256=project_sha256,
            source_sha256=source_sha256,
            restoration_status="complete",
            scan_range_covers_music=True,
            candidate_scan_truncated=False,
            retained_candidates=0,
        ),
    )


def _capture_side(
    album_side: AlbumSide,
    album_path: Path,
    *,
    reviewed_restoration: bool,
    captured_files: list[_CapturedFile],
    captured_catalogs: list[_CatalogCapture],
) -> _CapturedSide:
    project_path = resolve_album_reference(
        album_path,
        album_side.project,
        f"Side {album_side.label} project reference",
    )
    project_capture = _capture(
        project_path,
        label=f"Side {album_side.label} project",
    )
    project, project_sha256 = load_project_with_sha256(project_path)
    if project_sha256 != project_capture.receipt.sha256:
        raise ProjectValidationError(
            f"Side {album_side.label!r} project changed while it was loaded."
        )
    source_path = resolve_source_path(project, project_path).resolve()
    source_capture = _capture(
        source_path,
        label=f"Side {album_side.label} source",
    )
    if (
        source_capture.receipt.sha256 != project.source.sha256.lower()
        or source_capture.receipt.size_bytes != project.source.size_bytes
    ):
        raise ProjectValidationError(
            f"Side {album_side.label!r} source does not match its project."
        )
    identity = _approved_side_identity(
        album_side,
        project,
        project_sha256,
        source_capture.receipt.sha256,
    )
    restoration_render: RestorationRenderBinding | None = None
    restoration_no_derivative: RestorationNoDerivativeBinding | None = None
    if reviewed_restoration:
        restoration_render, restoration_no_derivative = _restoration_outcome(
            side_label=album_side.label,
            project_path=project_path,
            project_sha256=project_sha256,
            source_sha256=source_capture.receipt.sha256,
            plan_root=album_path.parent,
            captured_files=captured_files,
            captured_catalogs=captured_catalogs,
        )
    publication_side = PublicationSide(
        label=album_side.label,
        order=album_side.order,
        project_reference=album_side.project,
        current_identity=identity,
        selected_speed_state_sha256=album_side.speed.state.sha256,
        selected_effective_speed_factor=album_side.effective_speed_factor,
        restoration_render=restoration_render,
        restoration_no_derivative=restoration_no_derivative,
    ).normalized()
    captured_files.extend((project_capture, source_capture))
    return _CapturedSide(
        album_side,
        project_path,
        project,
        identity,
        publication_side,
    )


def _node(
    node_id: str,
    operation: str,
    observations: ToolObservations,
    settings: PublicationSettings,
    *,
    side_label: str | None = None,
    inputs: tuple[ProcessingInput, ...] = (),
    source_sample_rate: int | None = None,
    requested_speed_factor: float | None = None,
    restoration_mode: str | None = None,
) -> ProcessingNode:
    return ProcessingNode(
        node_id=node_id,
        operation=operation,
        side_label=side_label,
        inputs=inputs,
        tool=operation_tool_binding(
            operation,
            settings,
            observations,
            source_sample_rate=source_sample_rate,
            requested_speed_factor=requested_speed_factor,
            restoration_mode=restoration_mode,
        ),
    )


def _build_dag(
    sides: tuple[_CapturedSide, ...],
    profiles: tuple[str, ...],
    settings: PublicationSettings,
    observations: ToolObservations,
    *,
    restoration_mode: str,
) -> tuple[tuple[ProcessingNode, ...], tuple[ProfileOutput, ...]]:
    nodes: list[ProcessingNode] = []
    source_nodes: dict[str, ProcessingNode] = {}
    restored_nodes: dict[str, ProcessingNode] = {}
    corrected_nodes: dict[str, ProcessingNode] = {}
    for item in sides:
        order = item.album_side.order
        source = _node(
            f"source-{order:03d}",
            "source-side",
            observations,
            settings,
            side_label=item.album_side.label,
        )
        nodes.append(source)
        source_nodes[item.album_side.label] = source
        if item.publication_side.restoration_render is not None:
            restored = _node(
                f"restore-{order:03d}",
                "restore-side",
                observations,
                settings,
                side_label=item.album_side.label,
                inputs=(ProcessingInput("source", source.node_id),),
            )
            nodes.append(restored)
            restored_nodes[item.album_side.label] = restored

    if PROFILE_CORRECTED_LOSSLESS in profiles:
        for item in sides:
            label = item.album_side.label
            upstream = restored_nodes.get(label, source_nodes[label])
            corrected = _node(
                f"correct-{item.album_side.order:03d}",
                "correct-speed-side",
                observations,
                settings,
                side_label=label,
                inputs=(ProcessingInput("audio", upstream.node_id),),
                source_sample_rate=item.project.source.sample_rate,
                requested_speed_factor=item.album_side.effective_speed_factor,
                restoration_mode=restoration_mode,
            )
            nodes.append(corrected)
            corrected_nodes[label] = corrected

    outputs: list[ProfileOutput] = []
    if PROFILE_ARCHIVAL_SOURCE in profiles:
        archival = _node(
            "album-archival",
            "assemble-archival",
            observations,
            settings,
            inputs=tuple(
                ProcessingInput(
                    f"side-{item.album_side.order:03d}",
                    source_nodes[item.album_side.label].node_id,
                )
                for item in sides
            ),
        )
        nodes.append(archival)
        outputs.append(ProfileOutput(PROFILE_ARCHIVAL_SOURCE, archival.node_id))
    if PROFILE_RESTORED_SIDE in profiles:
        restored_album = _node(
            "album-restored",
            "assemble-restored",
            observations,
            settings,
            inputs=tuple(
                ProcessingInput(
                    f"side-{item.album_side.order:03d}",
                    restored_nodes.get(
                        item.album_side.label,
                        source_nodes[item.album_side.label],
                    ).node_id,
                )
                for item in sides
            ),
        )
        nodes.append(restored_album)
        outputs.append(ProfileOutput(PROFILE_RESTORED_SIDE, restored_album.node_id))
    if PROFILE_CORRECTED_LOSSLESS in profiles:
        lossless = _node(
            "album-corrected-lossless",
            "encode-lossless",
            observations,
            settings,
            inputs=tuple(
                ProcessingInput(
                    f"side-{item.album_side.order:03d}",
                    corrected_nodes[item.album_side.label].node_id,
                )
                for item in sides
            ),
        )
        nodes.append(lossless)
        outputs.append(ProfileOutput(PROFILE_CORRECTED_LOSSLESS, lossless.node_id))
        if PROFILE_PORTABLE in profiles:
            portable = _node(
                "album-portable",
                "encode-portable",
                observations,
                settings,
                inputs=(ProcessingInput("lossless", lossless.node_id),),
            )
            nodes.append(portable)
            outputs.append(ProfileOutput(PROFILE_PORTABLE, portable.node_id))
    return tuple(nodes), tuple(outputs)


def _revalidate_catalogs(captures: list[_CatalogCapture]) -> None:
    for captured in captures:
        current = discover_restoration_catalog(
            captured.workspace,
            captured.project_path,
            verified_source_sha256=captured.source_sha256,
        )
        if current != captured.catalog:
            raise ProjectValidationError(
                f"Side {captured.side_label!r} restoration workspace changed "
                "while the publication plan was being built."
            )


def build_album_publication_plan(
    album_path: Path | str,
    plan_path: Path | str,
    *,
    selected_profiles: Iterable[str],
    restoration_mode: str,
    flac_compression: int = 8,
    aac_bitrate_kbps: int = 256,
) -> AlbumPublicationPlan:
    """Build, revalidate, atomically save, and return one immutable plan."""

    resolved_album = _validated_album_path(Path(album_path))
    destination = _validated_destination(resolved_album, Path(plan_path))
    profiles = _selected_profiles(selected_profiles)
    reviewed_restoration = _validate_restoration_mode(profiles, restoration_mode)
    settings = PublicationSettings(flac_compression, aac_bitrate_kbps)
    settings.validate()
    observations = observe_publication_tools()

    captured_files: list[_CapturedFile] = []
    captured_catalogs: list[_CatalogCapture] = []
    album_capture = _capture(resolved_album, label="Album project")
    captured_files.append(album_capture)
    album, album_sha256 = load_album_project_with_sha256(resolved_album)
    if album_sha256 != album_capture.receipt.sha256:
        raise ProjectValidationError("Album project changed while it was being loaded.")
    album.validate()
    if album.artwork is not None:
        artwork_path = resolve_album_reference(
            resolved_album,
            album.artwork.path,
            "Album artwork path",
        )
        artwork_capture = _capture(artwork_path, label="Album artwork")
        if artwork_capture.receipt.sha256 != album.artwork.sha256:
            raise ProjectValidationError(
                "Album artwork no longer matches its approved SHA-256."
            )
        captured_files.append(artwork_capture)

    captured_sides = tuple(
        _capture_side(
            side,
            resolved_album,
            reviewed_restoration=reviewed_restoration,
            captured_files=captured_files,
            captured_catalogs=captured_catalogs,
        )
        for side in album.sides
    )
    nodes, outputs = _build_dag(
        captured_sides,
        profiles,
        settings,
        observations,
        restoration_mode=restoration_mode,
    )
    plan = AlbumPublicationPlan.create(
        album_reference=resolved_album.name,
        album_sha256=album_sha256,
        sides=(item.publication_side for item in captured_sides),
        selected_profiles=profiles,
        nodes=nodes,
        profile_outputs=outputs,
    )

    _revalidate_catalogs(captured_catalogs)
    if observe_publication_tools() != observations:
        raise ProjectValidationError(
            "Publication tool binaries or build observations changed during planning."
        )
    for item in captured_files:
        _assert_captured(item)
    _assert_destination_available(destination)
    save_album_publication_plan(plan, destination)
    return plan


__all__ = [
    "build_album_publication_plan",
    "default_restoration_workspace",
]
