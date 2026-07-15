"""Read-only verification, replay comparison, and owned-orphan recovery.

Verification never repairs or blesses a publication.  Replay always writes a
new no-overwrite output through the normal executor.  Recovery recognizes only
bounded direct-child stages whose strict ownership journal binds the current
directory identity.
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Mapping

from .album import AlbumProject, load_album_project_with_sha256, project_speed_state
from .album_publication_executor import (
    ALBUM_PUBLICATION_JOURNAL_SCHEMA,
    ALBUM_PUBLICATION_MANIFEST_SCHEMA,
    LEGACY_ALBUM_PUBLICATION_MANIFEST_SCHEMA,
    _JOURNAL_NAME,
    _MANIFEST_NAME,
    _DirectoryIdentity,
    _atomic_no_replace_directory,
    _audio_attestation,
    _directory_identity,
    _remove_owned_stage,
    _settings_from_plan,
    _source_object_filename,
    execute_album_publication_plan,
)
from .album_publication_navigation import (
    ALBUM_PUBLICATION_CHAPTERS_NAME,
    ALBUM_PUBLICATION_CHAPTERS_SCHEMA,
    ALBUM_PUBLICATION_CUE_NAME,
    NavigationSide,
    NavigationTrack,
    build_album_chapters,
    navigation_sides_from_publication,
    render_album_cue,
)
from .album_publication_plan import (
    AlbumPublicationPlan,
    load_album_publication_plan_with_sha256,
)
from .album_publication_policy import (
    ToolObservations,
    validate_operation_tool_binding,
)
from .errors import ExportError, GrooveSerpentError
from .exporter import _metadata_arguments
from .models import Project, Track
from .portable_names import portable_name_key
from .project_io import load_project_with_sha256
from .publication import (
    FileReceipt,
    assert_file_receipt,
    capture_file_receipt,
)


_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_JOURNAL_BYTES = 64 * 1024
_MAX_CHAPTERS_BYTES = 8 * 1024 * 1024
_MAX_TREE_ENTRIES = 100_000
_MAX_TREE_DEPTH = 32
_MAX_ORPHAN_CHILDREN = 4_096
_MAX_ORPHANS = 256
_REPARSE_POINT = 0x400
_DIGEST = re.compile(r"[0-9a-f]{64}")
_OPERATION_ID = re.compile(r"[0-9a-f]{32}")
_STAGE_NAME = re.compile(r"^\.groove-serpent-album-publication-([0-9a-f]{32})\.partial$")
_QUARANTINE_NAME = re.compile(r"^\.groove-serpent-album-cleanup-([0-9a-f]{32})\.partial$")
_WINDOWS_DEVICE_STEMS = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}

_COMMON_INVENTORY_FIELDS = {
    "path",
    "profile",
    "role",
    "size_bytes",
    "sha256",
}
_ROLE_FIELDS: dict[str, tuple[str, set[str], set[str]]] = {
    "full-capture-source": (
        "archival-source",
        {
            "source_object_id",
            "first_side_order",
            "verified_byte_identical",
        },
        {"verification"},
    ),
    "music-range-side": (
        "restored-side",
        {"side_order", "side_label", "verification"},
        set(),
    ),
    "corrected-track": (
        "corrected-lossless",
        {
            "side_order",
            "side_label",
            "local_track_number",
            "album_track_number",
            "source_start_sample",
            "source_end_sample",
            "relative_source_start_sample",
            "relative_source_end_sample",
            "corrected_start_sample",
            "corrected_end_sample",
            "requested_speed_factor",
            "effective_speed_factor",
            "asetrate_hz",
            "verification",
        },
        set(),
    ),
    "portable-track": (
        "portable",
        {
            "side_label",
            "album_track_number",
            "encoded_from",
            "lossless_input_sha256",
            "verification",
        },
        set(),
    ),
    "input-snapshot": ("provenance", set(), set()),
    "album-artwork": ("artwork", set(), set()),
    # Navigation integration adds these common-only roles without widening any
    # audio or provenance role's allowlist.
    "exact-chapters": (
        "navigation",
        {"schema", "precision"},
        set(),
    ),
    "approximate-cue": (
        "navigation",
        {"timebase_frames_per_second", "precision"},
        set(),
    ),
}
_LEGACY_FULL_CAPTURE_FIELDS = (
    "archival-source",
    {"side_order", "side_label", "verified_byte_identical"},
    {"verification"},
)

_AUDIO_ATTESTATION_FIELDS = {
    "codec_name",
    "sample_rate",
    "channels",
    "bits_per_raw_sample",
    "sample_format",
    "exact_sample_count",
    "presentation_sample_count",
    "decoded_pcm_sha256",
    "complete_decode_verified",
    "semantic_tags",
    "attached_picture_count",
    "embedded_artwork_sha256",
}
_VERIFICATION_FIELDS = {
    "codec_name",
    "sample_rate",
    "channels",
    "bits_per_raw_sample",
    "exact_sample_count",
    "presentation_sample_count",
    "decoded_pcm_sha256",
    "source_range_pcm_sha256",
    "complete_decode_verified",
    "validated_restoration_render",
    "reviewed_clean_pcm_equal",
    "audio_attestation",
}

_M4A_PRESERVED_SEMANTIC_TAGS = {
    "album",
    "album_artist",
    "artist",
    "comment",
    "date",
    "disc",
    "genre",
    "grouping",
    "title",
    "track",
}


@dataclass(frozen=True, slots=True)
class VerificationMismatch:
    code: str
    path: str | None
    expected: Any
    current: Any
    message: str


@dataclass(frozen=True, slots=True)
class AlbumPublicationVerificationReport:
    publication_directory: str
    ok: bool
    manifest_sha256: str | None
    journal_sha256: str | None
    artifact_count: int
    mismatches: tuple[VerificationMismatch, ...]


@dataclass(frozen=True, slots=True)
class AlbumPublicationReplayReport:
    original_directory: str
    replay_directory: str
    ok: bool
    mismatches: tuple[VerificationMismatch, ...]


@dataclass(frozen=True, slots=True)
class RecoveryDirectoryIdentity:
    device: int
    inode: int
    file_type: int
    birth_ns: int | None
    file_attributes: int | None

    @classmethod
    def from_internal(cls, value: _DirectoryIdentity) -> RecoveryDirectoryIdentity:
        return cls(**asdict(value))

    def internal(self) -> _DirectoryIdentity:
        return _DirectoryIdentity(**asdict(self))


@dataclass(frozen=True, slots=True)
class PublicationOrphan:
    path: str
    kind: str
    owned: bool
    state: str | None
    plan_sha256: str | None
    intended_output_name: str | None
    journal_sha256: str | None
    directory_identity: RecoveryDirectoryIdentity | None
    file_count: int
    total_size_bytes: int
    issue: str | None


@dataclass(frozen=True, slots=True)
class PublicationOrphanInventory:
    parent_directory: str
    orphans: tuple[PublicationOrphan, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class PublicationRecoveryReport:
    action: str
    original_path: str
    resulting_path: str | None
    removed: bool


@dataclass(frozen=True, slots=True)
class _VerifiedPublication:
    root: Path
    manifest: dict[str, Any]
    journal: dict[str, Any]
    inventory: tuple[dict[str, Any], ...]
    manifest_receipt: FileReceipt
    journal_receipt: FileReceipt
    plan: AlbumPublicationPlan
    projects: dict[str, Project]


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number {value!r} is forbidden.")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key!r}.")
        result[key] = value
    return result


def _strict_keys(
    value: Mapping[str, Any],
    required: set[str],
    label: str,
    *,
    optional: set[str] | None = None,
) -> None:
    keys = set(value)
    allowed = required | (optional or set())
    if keys != required and not (optional is not None and required <= keys <= allowed):
        missing = sorted(required - keys)
        extra = sorted(keys - allowed)
        raise ExportError(f"{label} fields are invalid (missing={missing}, extra={extra}).")


def _strict_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ExportError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _strict_integer(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = (1 << 63) - 1,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ExportError(f"{label} is outside its supported integer range.")
    return value


def _strict_text(value: Any, label: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ExportError(f"{label} must be bounded trimmed printable text.")
    return value


def _portable_relative(value: Any, label: str) -> str:
    text = _strict_text(value, label, maximum=4_096)
    if "\\" in text or text.startswith("/") or "//" in text or ":" in text:
        raise ExportError(f"{label} must be one portable relative path.")
    if any(character in '<>"|?*' for character in text):
        raise ExportError(f"{label} contains a non-portable character.")
    parts = text.split("/")
    if any(
        part in {"", ".", ".."}
        or len(part) > 255
        or part.endswith((" ", "."))
        or part.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_STEMS
        for part in parts
    ):
        raise ExportError(f"{label} is not a safe contained path.")
    if PurePosixPath(text).as_posix() != text:
        raise ExportError(f"{label} is not canonical POSIX path text.")
    return text


def _read_strict_json(
    path: Path,
    *,
    maximum_bytes: int,
    label: str,
) -> tuple[dict[str, Any], FileReceipt]:
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
    if metadata.st_size > maximum_bytes:
        raise ExportError(f"{label} exceeds its {maximum_bytes}-byte limit.")
    before = capture_file_receipt(path, label=label)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ExportError(f"{label} could not be read: {exc}") from exc
    if len(raw) > maximum_bytes or len(raw) != before.size_bytes:
        raise ExportError(f"{label} changed or exceeded its bounded size.")
    assert_file_receipt(path, before, label=label)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"{label} is not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ExportError(f"{label} root must be a JSON object.")
    return value, before


def load_album_publication_manifest(
    path: Path,
) -> tuple[dict[str, Any], FileReceipt]:
    """Load one bounded duplicate-key-free publication manifest."""

    value, receipt = _read_strict_json(
        path,
        maximum_bytes=_MAX_MANIFEST_BYTES,
        label="Album publication manifest",
    )
    common_fields = {
        "schema",
        "plan",
        "album",
        "selected_profiles",
        "restoration_mode",
        "tools",
        "processing_nodes",
        "sides",
        "inventory",
    }
    if value.get("schema") == ALBUM_PUBLICATION_MANIFEST_SCHEMA:
        expected_fields = common_fields | {"archival_sources"}
    elif value.get("schema") == LEGACY_ALBUM_PUBLICATION_MANIFEST_SCHEMA:
        expected_fields = common_fields
    else:
        raise ExportError("Album publication manifest schema is unsupported.")
    _strict_keys(value, expected_fields, "Album publication manifest")
    return value, receipt


def _identity_from_json(value: Any, label: str) -> _DirectoryIdentity:
    if not isinstance(value, dict):
        raise ExportError(f"{label} must be a JSON object.")
    _strict_keys(
        value,
        {"device", "inode", "file_type", "birth_ns", "file_attributes"},
        label,
    )
    device = _strict_integer(
        value["device"], f"{label} device", maximum=(1 << 64) - 1
    )
    inode = _strict_integer(
        value["inode"], f"{label} inode", maximum=(1 << 64) - 1
    )
    file_type = _strict_integer(value["file_type"], f"{label} file type")
    for name in ("birth_ns", "file_attributes"):
        if value[name] is not None:
            _strict_integer(value[name], f"{label} {name}", maximum=(1 << 64) - 1)
    return _DirectoryIdentity(
        device=device,
        inode=inode,
        file_type=file_type,
        birth_ns=value["birth_ns"],
        file_attributes=value["file_attributes"],
    )


def load_album_publication_journal(
    path: Path,
) -> tuple[dict[str, Any], FileReceipt, _DirectoryIdentity]:
    """Load one bounded duplicate-key-free ownership journal."""

    value, receipt = _read_strict_json(
        path,
        maximum_bytes=_MAX_JOURNAL_BYTES,
        label="Album publication journal",
    )
    _strict_keys(
        value,
        {
            "schema",
            "state",
            "plan_sha256",
            "operation_id",
            "original_stage_name",
            "intended_output_name",
            "stage_identity",
        },
        "Album publication journal",
    )
    if value["schema"] != ALBUM_PUBLICATION_JOURNAL_SCHEMA:
        raise ExportError("Album publication journal schema is unsupported.")
    if value["state"] not in {"staging", "verified-ready"}:
        raise ExportError("Album publication journal state is unsupported.")
    _strict_digest(value["plan_sha256"], "Journal plan SHA-256")
    operation_id = _strict_text(value["operation_id"], "Journal operation ID")
    if _OPERATION_ID.fullmatch(operation_id) is None:
        raise ExportError("Journal operation ID is invalid.")
    expected_stage = f".groove-serpent-album-publication-{operation_id}.partial"
    if value["original_stage_name"] != expected_stage:
        raise ExportError("Journal original stage name does not match its operation ID.")
    output_name = _strict_text(value["intended_output_name"], "Intended output name")
    if Path(output_name).name != output_name or portable_name_key(output_name) in {
        portable_name_key(_MANIFEST_NAME),
        portable_name_key(_JOURNAL_NAME),
    }:
        raise ExportError("Journal intended output name is invalid.")
    identity = _identity_from_json(value["stage_identity"], "Journal stage identity")
    return value, receipt, identity


def _walk_tree(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    global_keys: set[str] = set()
    pending = [(root, 0)]
    count = 0
    while pending:
        directory, depth = pending.pop()
        relative_directory = directory.relative_to(root).as_posix() if directory != root else ""
        if relative_directory:
            directories.add(relative_directory)
        local_keys: set[str] = set()
        try:
            entries = os.scandir(directory)
        except OSError as exc:
            raise ExportError("Publication tree could not be enumerated.") from exc
        with entries:
            for entry in entries:
                count += 1
                if count > _MAX_TREE_ENTRIES:
                    raise ExportError("Publication tree contains too many entries.")
                local_key = portable_name_key(entry.name)
                if local_key in local_keys:
                    raise ExportError(
                        "Publication tree contains a portable-equivalent name collision."
                    )
                local_keys.add(local_key)
                path = Path(entry.path)
                try:
                    metadata = path.lstat()
                except OSError as exc:
                    raise ExportError("Publication entry could not be inspected.") from exc
                attributes = int(getattr(metadata, "st_file_attributes", 0))
                if stat.S_ISLNK(metadata.st_mode) or attributes & _REPARSE_POINT:
                    raise ExportError("Publication tree contains a symlink or reparse point.")
                relative = path.relative_to(root).as_posix()
                portable_key = portable_name_key(relative)
                if portable_key in global_keys:
                    raise ExportError(
                        "Publication tree contains a portable-equivalent path collision."
                    )
                global_keys.add(portable_key)
                if stat.S_ISDIR(metadata.st_mode):
                    if depth >= _MAX_TREE_DEPTH:
                        raise ExportError("Publication tree is nested too deeply.")
                    pending.append((path, depth + 1))
                elif stat.S_ISREG(metadata.st_mode):
                    files.add(relative)
                else:
                    raise ExportError("Publication tree contains an unsafe file type.")
    return files, directories


def _validate_audio_attestation(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExportError(f"{label} must be a JSON object.")
    _strict_keys(value, _AUDIO_ATTESTATION_FIELDS, label)
    if value["codec_name"] not in {"flac", "aac"}:
        raise ExportError(f"{label} codec is unsupported.")
    for field in ("sample_rate", "channels", "exact_sample_count"):
        _strict_integer(value[field], f"{label} {field}", minimum=1)
    bits = value["bits_per_raw_sample"]
    if bits is not None:
        _strict_integer(bits, f"{label} bit depth", minimum=1, maximum=64)
    if value["presentation_sample_count"] is not None:
        _strict_integer(
            value["presentation_sample_count"],
            f"{label} presentation sample count",
            minimum=1,
        )
    _strict_digest(value["decoded_pcm_sha256"], f"{label} decoded PCM SHA-256")
    if value["complete_decode_verified"] is not True:
        raise ExportError(f"{label} must record complete decode verification.")
    tags = value["semantic_tags"]
    if not isinstance(tags, dict) or len(tags) > 64:
        raise ExportError(f"{label} semantic tags are invalid.")
    for key, tag_value in tags.items():
        _strict_text(key, f"{label} tag key", maximum=128)
        if not isinstance(tag_value, str) or len(tag_value) > 4_096:
            raise ExportError(f"{label} tag value is invalid.")
    attached = _strict_integer(
        value["attached_picture_count"],
        f"{label} attached picture count",
        maximum=1,
    )
    artwork_sha = value["embedded_artwork_sha256"]
    if attached == 0 and artwork_sha is not None:
        raise ExportError(f"{label} records artwork without an attached picture.")
    if attached == 1:
        _strict_digest(artwork_sha, f"{label} artwork SHA-256")
    return value


def _validate_verification(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExportError(f"{label} must be a JSON object.")
    keys = set(value)
    if "audio_attestation" not in keys or not keys <= _VERIFICATION_FIELDS:
        raise ExportError(f"{label} fields are invalid.")
    _validate_audio_attestation(value["audio_attestation"], f"{label} attestation")
    for digest_field in ("decoded_pcm_sha256", "source_range_pcm_sha256"):
        if value.get(digest_field) is not None:
            _strict_digest(value[digest_field], f"{label} {digest_field}")
    return value


def _expected_semantic_tags(
    track: Track,
    total_tracks: int,
    album_metadata: Mapping[str, str],
    *,
    portable: bool,
) -> dict[str, str]:
    arguments = _metadata_arguments(
        track,
        total_tracks,
        project_metadata=album_metadata,
    )
    if len(arguments) % 2 != 0:
        raise ExportError("Publication metadata arguments are internally inconsistent.")
    tags: dict[str, str] = {}
    for option, assignment in zip(arguments[::2], arguments[1::2], strict=True):
        if option != "-metadata" or "=" not in assignment:
            raise ExportError("Publication metadata arguments are internally inconsistent.")
        key, value = assignment.split("=", 1)
        if not portable or key in _M4A_PRESERVED_SEMANTIC_TAGS:
            tags[key] = value
    return {key: tags[key] for key in sorted(tags)}


def _album_output_track(
    source: Track,
    *,
    album_track_number: int,
    side_label: str,
    album_metadata: Mapping[str, str],
) -> Track:
    track = Track.from_dict(asdict(source))
    track.number = album_track_number
    track.side = side_label
    for field_name, value in (
        ("artist", album_metadata.get("artist", "")),
        ("album", album_metadata.get("album") or album_metadata.get("title", "")),
        ("album_artist", album_metadata.get("album_artist", "")),
        ("year", album_metadata.get("year", "")),
        ("genre", album_metadata.get("genre", "")),
    ):
        if value:
            setattr(track, field_name, value)
    return track


def _side_output_track(project: Project, side_label: str) -> Track:
    first = project.tracks[0]
    title = project.metadata.get("album") or project.metadata.get("title")
    album_title = title or first.album or "Album"
    return Track(
        number=1,
        title=f"{album_title} - Side {side_label}",
        start_sample=project.tracks[0].start_sample,
        end_sample=project.tracks[-1].end_sample,
        start_seconds=project.tracks[0].start_seconds,
        end_seconds=project.tracks[-1].end_seconds,
        artist=first.artist,
        album=first.album,
        album_artist=first.album_artist,
        year=first.year,
        genre=first.genre,
        side=side_label,
    )


def _audio_attestation_from_item(item: Mapping[str, Any]) -> Mapping[str, Any]:
    verification = item.get("verification")
    if not isinstance(verification, dict):
        raise ExportError("Publication audio inventory has no verification object.")
    attestation = verification.get("audio_attestation")
    if not isinstance(attestation, dict):
        raise ExportError("Publication audio inventory has no attestation object.")
    return attestation


def _assert_generated_audio_semantics(
    item: Mapping[str, Any],
    *,
    expected_tags: Mapping[str, str],
    expected_artwork_sha256: str | None,
) -> None:
    attestation = _audio_attestation_from_item(item)
    current_tags = attestation.get("semantic_tags")
    if current_tags != dict(expected_tags):
        raise ExportError(
            f"Generated audio tags differ from immutable provenance for {item['path']!r}."
        )
    expected_count = 1 if expected_artwork_sha256 is not None else 0
    if (
        attestation.get("attached_picture_count") != expected_count
        or attestation.get("embedded_artwork_sha256") != expected_artwork_sha256
    ):
        raise ExportError(
            f"Generated audio artwork differs from immutable provenance for {item['path']!r}."
        )


def _items_with_role(
    inventory: tuple[dict[str, Any], ...],
    role: str,
) -> tuple[dict[str, Any], ...]:
    return tuple(item for item in inventory if item["role"] == role)


def _indexed_side_items(
    items: tuple[dict[str, Any], ...],
    *,
    role: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        label = item.get("side_label")
        if not isinstance(label, str) or label in result:
            raise ExportError(f"Publication role {role!r} repeats a side identity.")
        result[label] = item
    return result


def _validate_inventory_provenance(
    inventory: tuple[dict[str, Any], ...],
    *,
    plan: AlbumPublicationPlan,
    album: AlbumProject,
    projects: Mapping[str, Project],
    plan_raw_sha256: str,
    album_sha256: str,
) -> None:
    expected_provenance = {
        "provenance/publication-plan.json": plan_raw_sha256,
        "provenance/album-project.json": album_sha256,
    }
    for side in plan.sides:
        prefix = f"provenance/sides/{side.order:02d}"
        expected_provenance[f"{prefix}/project.groove.json"] = side.current_identity.project_sha256
        if side.restoration_render is not None:
            expected_provenance[f"{prefix}/restoration/render-manifest.json"] = (
                side.restoration_render.manifest_sha256
            )
        elif side.restoration_no_derivative is not None:
            expected_provenance[f"{prefix}/restoration/clean-scan.json"] = (
                side.restoration_no_derivative.scan_sha256
            )
    provenance_items = {
        str(item["path"]): item for item in _items_with_role(inventory, "input-snapshot")
    }
    if set(provenance_items) != set(expected_provenance):
        raise ExportError("Publication provenance inventory paths are incomplete or extra.")
    for path, expected_sha256 in expected_provenance.items():
        if provenance_items[path]["sha256"] != expected_sha256:
            raise ExportError(f"Publication provenance artifact {path!r} has wrong identity.")

    artwork_items = _items_with_role(inventory, "album-artwork")
    if album.artwork is None:
        if artwork_items:
            raise ExportError("Publication contains artwork absent from the album project.")
    else:
        expected_path = f"artwork/cover{Path(album.artwork.path).suffix.casefold()}"
        if (
            len(artwork_items) != 1
            or artwork_items[0]["path"] != expected_path
            or artwork_items[0]["sha256"] != album.artwork.sha256
        ):
            raise ExportError("Publication artwork differs from immutable album provenance.")

    planned_labels = {side.label for side in plan.sides}
    if set(projects) != planned_labels:
        raise ExportError("Verified project set differs from immutable plan sides.")


def _validate_legacy_archival_sources(
    inventory: tuple[dict[str, Any], ...],
    *,
    plan: AlbumPublicationPlan,
    projects: Mapping[str, Project],
) -> None:
    selected = set(plan.selected_profiles)
    archival = _items_with_role(inventory, "full-capture-source")
    expected_archival = len(plan.sides) if "archival-source" in selected else 0
    if len(archival) != expected_archival:
        raise ExportError("Archival source inventory count differs from selected profiles.")
    archival_by_side = _indexed_side_items(archival, role="full-capture-source")
    for side in plan.sides:
        item = archival_by_side.get(side.label)
        if item is None:
            continue
        project = projects[side.label]
        if (
            item["side_order"] != side.order
            or item["verified_byte_identical"] is not True
            or item["sha256"] != side.current_identity.source_sha256
            or item["size_bytes"] != project.source.size_bytes
        ):
            raise ExportError(
                f"Archival Side {side.label} is not byte-bound to immutable provenance."
            )


def _validate_archival_source_ledger(
    value: Any,
    inventory: tuple[dict[str, Any], ...],
    *,
    plan: AlbumPublicationPlan,
    projects: Mapping[str, Project],
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ExportError("Archival source ledger must be a JSON object.")
    _strict_keys(value, {"objects", "side_bindings"}, "Archival source ledger")
    raw_objects = value["objects"]
    raw_bindings = value["side_bindings"]
    if (
        not isinstance(raw_objects, list)
        or len(raw_objects) > len(plan.sides)
        or not isinstance(raw_bindings, list)
        or len(raw_bindings) > len(plan.sides)
    ):
        raise ExportError("Archival source ledger arrays are invalid or unbounded.")

    selected = set(plan.selected_profiles)
    archival = _items_with_role(inventory, "full-capture-source")
    if "archival-source" not in selected:
        if archival or raw_objects or raw_bindings:
            raise ExportError("Archival source ledger exists without the selected profile.")
        return {}

    expected_groups: list[dict[str, Any]] = []
    group_by_identity: dict[tuple[str, int], dict[str, Any]] = {}
    expected_binding_by_label: dict[str, dict[str, Any]] = {}
    for side in plan.sides:
        project = projects[side.label]
        identity = (
            side.current_identity.source_sha256,
            project.source.size_bytes,
        )
        group = group_by_identity.get(identity)
        if group is None:
            object_id = (
                f"source-{len(expected_groups) + 1:02d}-{side.current_identity.source_sha256[:12]}"
            )
            group = {
                "object_id": object_id,
                "source_sha256": side.current_identity.source_sha256,
                "source_size_bytes": project.source.size_bytes,
                "first_side_order": side.order,
                "first_side_label": side.label,
                "side_labels": [],
            }
            group_by_identity[identity] = group
            expected_groups.append(group)
        group["side_labels"].append(side.label)
        expected_binding_by_label[side.label] = group

    if len(archival) != len(expected_groups):
        raise ExportError("Archival object count differs from unique immutable source identities.")
    inventory_by_object: dict[str, dict[str, Any]] = {}
    for item in archival:
        object_id = _strict_text(
            item.get("source_object_id"),
            "Archival inventory source object ID",
        )
        if object_id in inventory_by_object:
            raise ExportError("Archival inventory repeats a source object ID.")
        _strict_integer(
            item.get("first_side_order"),
            "Archival inventory first side order",
            minimum=1,
        )
        if item.get("verified_byte_identical") is not True:
            raise ExportError("Archival inventory lacks byte-identical verification.")
        inventory_by_object[object_id] = item

    if len(raw_objects) != len(expected_groups):
        raise ExportError("Archival source object ledger has the wrong count.")
    expected_object_ids: set[str] = set()
    for raw_object, expected in zip(raw_objects, expected_groups, strict=True):
        if not isinstance(raw_object, dict):
            raise ExportError("Every archival source object must be a JSON object.")
        _strict_keys(
            raw_object,
            {
                "object_id",
                "path",
                "source_sha256",
                "source_size_bytes",
                "first_side_order",
                "verified_byte_identical",
            },
            "Archival source object",
        )
        object_id = _strict_text(raw_object["object_id"], "Source object ID")
        path = _portable_relative(raw_object["path"], "Source object path")
        _strict_digest(raw_object["source_sha256"], "Source object SHA-256")
        _strict_integer(
            raw_object["source_size_bytes"],
            "Source object size",
        )
        _strict_integer(
            raw_object["first_side_order"],
            "Source object first side order",
            minimum=1,
        )
        if raw_object["verified_byte_identical"] is not True:
            raise ExportError("Source object must record byte-identical verification.")
        if not path.startswith("archival-source/"):
            raise ExportError("Source object path is outside archival-source.")
        if object_id != expected["object_id"]:
            raise ExportError("Source object IDs are not canonical and deterministic.")
        expected_project = projects[str(expected["first_side_label"])]
        expected_path = "archival-source/" + _source_object_filename(
            object_id,
            Path(expected_project.source.filename),
        )
        inventory_item = inventory_by_object.get(object_id)
        if inventory_item is None:
            raise ExportError("Source object has no matching archival inventory artifact.")
        if (
            raw_object["path"] != expected_path
            or raw_object["path"] != inventory_item["path"]
            or raw_object["source_sha256"] != inventory_item["sha256"]
            or raw_object["source_size_bytes"] != inventory_item["size_bytes"]
            or raw_object["first_side_order"] != inventory_item["first_side_order"]
            or raw_object["verified_byte_identical"] != inventory_item["verified_byte_identical"]
            or raw_object["source_sha256"] != expected["source_sha256"]
            or raw_object["source_size_bytes"] != expected["source_size_bytes"]
            or raw_object["first_side_order"] != expected["first_side_order"]
        ):
            raise ExportError(
                "Source object ledger differs from inventory or immutable provenance."
            )
        expected_object_ids.add(object_id)
    if set(inventory_by_object) != expected_object_ids:
        raise ExportError("Archival inventory contains an unbound source object.")

    if len(raw_bindings) != len(plan.sides):
        raise ExportError("Archival side-binding ledger has the wrong count.")
    bindings: dict[str, str] = {}
    for raw_binding, side in zip(raw_bindings, plan.sides, strict=True):
        if not isinstance(raw_binding, dict):
            raise ExportError("Every archival side binding must be a JSON object.")
        _strict_keys(
            raw_binding,
            {
                "side_order",
                "side_label",
                "side_project_sha256",
                "source_object_id",
                "source_sha256",
                "source_size_bytes",
            },
            "Archival side binding",
        )
        expected = expected_binding_by_label[side.label]
        _strict_integer(raw_binding["side_order"], "Binding side order", minimum=1)
        _strict_text(raw_binding["side_label"], "Binding side label")
        _strict_digest(
            raw_binding["side_project_sha256"],
            "Binding side project SHA-256",
        )
        _strict_text(raw_binding["source_object_id"], "Binding source object ID")
        _strict_digest(raw_binding["source_sha256"], "Binding source SHA-256")
        _strict_integer(raw_binding["source_size_bytes"], "Binding source size")
        if (
            raw_binding["side_order"] != side.order
            or raw_binding["side_label"] != side.label
            or raw_binding["side_project_sha256"] != side.current_identity.project_sha256
            or raw_binding["source_object_id"] != expected["object_id"]
            or raw_binding["source_sha256"] != expected["source_sha256"]
            or raw_binding["source_size_bytes"] != expected["source_size_bytes"]
            or raw_binding["source_object_id"] not in expected_object_ids
        ):
            raise ExportError(
                f"Archival Side {side.label} binding differs from immutable provenance."
            )
        bindings[side.label] = str(raw_binding["source_object_id"])
    if set(bindings) != {side.label for side in plan.sides}:
        raise ExportError("Archival side bindings are incomplete or repeated.")
    return bindings


def _validate_audio_role_semantics(
    inventory: tuple[dict[str, Any], ...],
    *,
    plan: AlbumPublicationPlan,
    album: AlbumProject,
    projects: Mapping[str, Project],
    manifest_schema: str,
    archival_sources: Any,
) -> dict[str, str] | None:
    selected = set(plan.selected_profiles)
    artwork_sha256 = album.artwork.sha256 if album.artwork is not None else None
    total_tracks = sum(len(projects[side.label].tracks) for side in plan.sides)

    if manifest_schema == LEGACY_ALBUM_PUBLICATION_MANIFEST_SCHEMA:
        _validate_legacy_archival_sources(
            inventory,
            plan=plan,
            projects=projects,
        )
        archival_bindings: dict[str, str] | None = None
    else:
        archival_bindings = _validate_archival_source_ledger(
            archival_sources,
            inventory,
            plan=plan,
            projects=projects,
        )

    restored = _items_with_role(inventory, "music-range-side")
    expected_restored = len(plan.sides) if "restored-side" in selected else 0
    if len(restored) != expected_restored:
        raise ExportError("Restored-side inventory count differs from selected profiles.")
    restored_by_side = _indexed_side_items(restored, role="music-range-side")
    for side in plan.sides:
        item = restored_by_side.get(side.label)
        if item is None:
            continue
        if item["side_order"] != side.order:
            raise ExportError(f"Restored Side {side.label} has the wrong side order.")
        verification = item["verification"]
        if side.restoration_render is not None:
            if (
                item["sha256"] != side.restoration_render.audio_sha256
                or verification.get("validated_restoration_render") is not True
            ):
                raise ExportError(
                    f"Restored Side {side.label} is not byte-bound to its reviewed render."
                )
        elif side.restoration_no_derivative is not None:
            if verification.get("reviewed_clean_pcm_equal") is not True:
                raise ExportError(f"Clean Side {side.label} lacks its reviewed pass-through proof.")
            expected_tags = _expected_semantic_tags(
                _side_output_track(projects[side.label], side.label),
                1,
                album.metadata,
                portable=False,
            )
            _assert_generated_audio_semantics(
                item,
                expected_tags=expected_tags,
                expected_artwork_sha256=None,
            )
        else:
            raise ExportError(
                f"Restored Side {side.label} has no immutable reviewed restoration outcome."
            )

    album_tracks: dict[int, tuple[str, int, Track]] = {}
    album_number = 0
    for side in plan.sides:
        project = projects[side.label]
        for local_number, source_track in enumerate(project.tracks, start=1):
            album_number += 1
            album_tracks[album_number] = (side.label, local_number, source_track)

    corrected = _items_with_role(inventory, "corrected-track")
    expected_corrected = total_tracks if "corrected-lossless" in selected else 0
    if len(corrected) != expected_corrected:
        raise ExportError("Corrected-track inventory count differs from selected profiles.")
    corrected_by_number: dict[int, dict[str, Any]] = {}
    for item in corrected:
        number = item["album_track_number"]
        if type(number) is not int or number in corrected_by_number:
            raise ExportError("Corrected-track inventory repeats album numbering.")
        expected = album_tracks.get(number)
        if expected is None:
            raise ExportError("Corrected-track inventory has an unknown album number.")
        side_label, local_number, source_track = expected
        planned_side = next(side for side in plan.sides if side.label == side_label)
        if (
            item["side_label"] != side_label
            or item["side_order"] != planned_side.order
            or item["local_track_number"] != local_number
            or item["source_start_sample"] != source_track.start_sample
            or item["source_end_sample"] != source_track.end_sample
        ):
            raise ExportError("Corrected-track identity differs from immutable provenance.")
        track = _album_output_track(
            source_track,
            album_track_number=number,
            side_label=side_label,
            album_metadata=album.metadata,
        )
        _assert_generated_audio_semantics(
            item,
            expected_tags=_expected_semantic_tags(
                track,
                total_tracks,
                album.metadata,
                portable=False,
            ),
            expected_artwork_sha256=artwork_sha256,
        )
        corrected_by_number[number] = item

    portable = _items_with_role(inventory, "portable-track")
    expected_portable = total_tracks if "portable" in selected else 0
    if len(portable) != expected_portable:
        raise ExportError("Portable-track inventory count differs from selected profiles.")
    portable_numbers: set[int] = set()
    for item in portable:
        number = item["album_track_number"]
        if type(number) is not int or number in portable_numbers:
            raise ExportError("Portable-track inventory repeats album numbering.")
        expected = album_tracks.get(number)
        if expected is None:
            raise ExportError("Portable-track inventory has an unknown album number.")
        side_label, _local_number, source_track = expected
        if (
            item["side_label"] != side_label
            or item["encoded_from"] != "staged-corrected-lossless-flac"
        ):
            raise ExportError("Portable-track identity differs from immutable provenance.")
        if (
            number in corrected_by_number
            and item["lossless_input_sha256"] != corrected_by_number[number]["sha256"]
        ):
            raise ExportError("Portable-track lossless input identity is inconsistent.")
        track = _album_output_track(
            source_track,
            album_track_number=number,
            side_label=side_label,
            album_metadata=album.metadata,
        )
        _assert_generated_audio_semantics(
            item,
            expected_tags=_expected_semantic_tags(
                track,
                total_tracks,
                album.metadata,
                portable=True,
            ),
            expected_artwork_sha256=artwork_sha256,
        )
        portable_numbers.add(number)
    return archival_bindings


def _validate_inventory_item(
    value: Any,
    *,
    manifest_schema: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExportError("Every publication inventory item must be a JSON object.")
    role = value.get("role")
    if not isinstance(role, str) or role not in _ROLE_FIELDS:
        raise ExportError(f"Unsupported publication inventory role {role!r}.")
    role_fields = _ROLE_FIELDS[role]
    if (
        manifest_schema == LEGACY_ALBUM_PUBLICATION_MANIFEST_SCHEMA
        and role == "full-capture-source"
    ):
        role_fields = _LEGACY_FULL_CAPTURE_FIELDS
    expected_profile, required_extra, optional_extra = role_fields
    required = _COMMON_INVENTORY_FIELDS | required_extra
    _strict_keys(value, required, f"Inventory role {role}", optional=optional_extra)
    if role in {"exact-chapters", "approximate-cue"}:
        if value["profile"] not in {
            "archival-source",
            "restored-side",
            "corrected-lossless",
            "navigation",
        }:
            raise ExportError("Navigation inventory profile is invalid.")
    elif value["profile"] != expected_profile:
        raise ExportError(f"Inventory role {role!r} has the wrong profile.")
    _portable_relative(value["path"], "Inventory path")
    _strict_integer(value["size_bytes"], "Inventory file size")
    _strict_digest(value["sha256"], "Inventory SHA-256")
    if str(value["path"]).casefold().endswith((".flac", ".m4a")):
        _validate_verification(value.get("verification"), "Audio verification")
    return value


def _tool_observations(value: Any) -> ToolObservations:
    if not isinstance(value, dict):
        raise ExportError("Manifest tool observations must be a JSON object.")
    fields = set(ToolObservations.__dataclass_fields__)
    _strict_keys(value, fields, "Manifest tool observations")
    observations = ToolObservations(**value)
    observations.validate()
    return observations


def _validate_navigation(
    root: Path,
    inventory_by_path: Mapping[str, dict[str, Any]],
    manifest: Mapping[str, Any],
) -> None:
    chapter_items = [
        item for item in inventory_by_path.values() if item["role"] == "exact-chapters"
    ]
    cue_items = [item for item in inventory_by_path.values() if item["role"] == "approximate-cue"]
    if not chapter_items and not cue_items:
        return
    if len(chapter_items) != 1 or len(cue_items) != 1:
        raise ExportError("Publication navigation requires one chapters file and one CUE.")
    chapters_item = chapter_items[0]
    cue_item = cue_items[0]
    if Path(str(chapters_item["path"])).name != ALBUM_PUBLICATION_CHAPTERS_NAME:
        raise ExportError("Exact chapters artifact has the wrong filename.")
    if Path(str(cue_item["path"])).name != ALBUM_PUBLICATION_CUE_NAME:
        raise ExportError("Approximate CUE artifact has the wrong filename.")
    chapters, _receipt = _read_strict_json(
        root / Path(str(chapters_item["path"])),
        maximum_bytes=_MAX_CHAPTERS_BYTES,
        label="Album publication chapters",
    )
    _strict_keys(
        chapters,
        {
            "schema",
            "plan_sha256",
            "album_project_sha256",
            "basis_profile",
            "metadata",
            "precision",
            "cue_companion",
            "total_tracks",
            "sides",
        },
        "Album publication chapters",
    )
    if chapters["schema"] != ALBUM_PUBLICATION_CHAPTERS_SCHEMA:
        raise ExportError("Album publication chapters schema is unsupported.")
    if chapters["plan_sha256"] != manifest["plan"]["plan_sha256"]:
        raise ExportError("Chapters plan identity differs from the manifest.")
    if chapters["album_project_sha256"] != manifest["album"]["sha256"]:
        raise ExportError("Chapters album identity differs from the manifest.")
    raw_sides = chapters["sides"]
    if not isinstance(raw_sides, list):
        raise ExportError("Chapters sides must be a JSON array.")
    sides: list[NavigationSide] = []
    for raw_side in raw_sides:
        if not isinstance(raw_side, dict):
            raise ExportError("Every chapters side must be a JSON object.")
        _strict_keys(
            raw_side,
            {
                "order",
                "label",
                "timeline_origin",
                "source_sample_rate",
                "output_sample_rate",
                "output_start_sample",
                "output_end_sample_exclusive",
                "tracks",
            },
            "Chapters side",
        )
        raw_tracks = raw_side["tracks"]
        if not isinstance(raw_tracks, list):
            raise ExportError("Chapters tracks must be a JSON array.")
        tracks: list[NavigationTrack] = []
        for raw_track in raw_tracks:
            if not isinstance(raw_track, dict):
                raise ExportError("Every chapters track must be a JSON object.")
            _strict_keys(
                raw_track,
                {
                    "album_track_number",
                    "local_track_number",
                    "title",
                    "artist",
                    "file",
                    "source_start_sample",
                    "source_end_sample_exclusive",
                    "side_output_start_sample",
                    "side_output_end_sample_exclusive",
                },
                "Chapters track",
            )
            file_value = raw_track["file"]
            if not isinstance(file_value, dict):
                raise ExportError("Chapters file binding must be a JSON object.")
            _strict_keys(
                file_value,
                {"path", "sha256", "sample_count", "start_sample", "end_sample_exclusive"},
                "Chapters file binding",
            )
            referenced = inventory_by_path.get(str(file_value["path"]))
            if referenced is None or referenced["sha256"] != file_value["sha256"]:
                raise ExportError("Chapters reference an unknown audio file identity.")
            tracks.append(
                NavigationTrack(
                    album_track_number=raw_track["album_track_number"],
                    local_track_number=raw_track["local_track_number"],
                    title=raw_track["title"],
                    artist=raw_track["artist"],
                    file_path=file_value["path"],
                    file_sha256=file_value["sha256"],
                    file_sample_count=file_value["sample_count"],
                    source_start_sample=raw_track["source_start_sample"],
                    source_end_sample=raw_track["source_end_sample_exclusive"],
                    side_output_start_sample=raw_track["side_output_start_sample"],
                    side_output_end_sample=raw_track["side_output_end_sample_exclusive"],
                    file_output_start_sample=file_value["start_sample"],
                    file_output_end_sample=file_value["end_sample_exclusive"],
                )
            )
        side = NavigationSide(
            order=raw_side["order"],
            label=raw_side["label"],
            source_sample_rate=raw_side["source_sample_rate"],
            output_sample_rate=raw_side["output_sample_rate"],
            timeline_origin=raw_side["timeline_origin"],
            tracks=tuple(tracks),
        )
        side.validate()
        if (
            raw_side["output_start_sample"] != side.tracks[0].side_output_start_sample
            or raw_side["output_end_sample_exclusive"] != side.tracks[-1].side_output_end_sample
        ):
            raise ExportError("Chapters side summary differs from exact tracks.")
        sides.append(side)
    rebuilt = build_album_chapters(
        plan_sha256=chapters["plan_sha256"],
        album_sha256=chapters["album_project_sha256"],
        basis_profile=chapters["basis_profile"],
        metadata=chapters["metadata"],
        sides=sides,
    )
    if rebuilt != chapters:
        raise ExportError("Album chapters are not canonical or internally consistent.")
    try:
        cue_text = (root / Path(str(cue_item["path"]))).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExportError("Approximate CUE could not be read as UTF-8.") from exc
    if cue_text != render_album_cue(metadata=chapters["metadata"], sides=sides):
        raise ExportError("Approximate CUE differs from exact chapters geometry.")


def _validate_navigation_from_provenance(
    root: Path,
    inventory_by_path: Mapping[str, dict[str, Any]],
    manifest: Mapping[str, Any],
    *,
    album: AlbumProject,
    projects: Mapping[str, Project],
    archival_source_bindings: Mapping[str, str] | None,
) -> None:
    chapter_items = [
        item for item in inventory_by_path.values() if item["role"] == "exact-chapters"
    ]
    cue_items = [item for item in inventory_by_path.values() if item["role"] == "approximate-cue"]
    if len(chapter_items) != 1 or len(cue_items) != 1:
        raise ExportError("A final publication requires one exact chapters file and one CUE.")
    chapters_item = chapter_items[0]
    cue_item = cue_items[0]
    chapters_path = root / Path(str(chapters_item["path"]))
    cue_path = root / Path(str(cue_item["path"]))
    if chapters_path.name != ALBUM_PUBLICATION_CHAPTERS_NAME:
        raise ExportError("Exact chapters artifact has the wrong filename.")
    if cue_path.name != ALBUM_PUBLICATION_CUE_NAME:
        raise ExportError("Approximate CUE artifact has the wrong filename.")
    chapters, _receipt = _read_strict_json(
        chapters_path,
        maximum_bytes=_MAX_CHAPTERS_BYTES,
        label="Album publication chapters",
    )
    basis, expected_sides = navigation_sides_from_publication(
        album=album,
        projects_by_label=projects,
        selected_profiles=manifest["selected_profiles"],
        inventory=inventory_by_path.values(),
        archival_source_bindings=archival_source_bindings,
    )
    expected_chapters = build_album_chapters(
        plan_sha256=manifest["plan"]["plan_sha256"],
        album_sha256=manifest["album"]["sha256"],
        basis_profile=basis,
        metadata=dict(album.metadata),
        sides=expected_sides,
    )
    if chapters != expected_chapters:
        raise ExportError("Exact chapters differ from verified provenance and audio inventory.")
    expected_chapter_bytes = (
        json.dumps(
            expected_chapters,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    try:
        current_chapter_bytes = chapters_path.read_bytes()
        current_cue = cue_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExportError("Publication navigation could not be read exactly.") from exc
    if current_chapter_bytes != expected_chapter_bytes:
        raise ExportError("Exact chapters JSON is not canonical byte-for-byte.")
    expected_cue = render_album_cue(
        metadata=dict(album.metadata),
        sides=expected_sides,
    )
    if current_cue != expected_cue:
        raise ExportError("Approximate CUE differs from verified exact geometry.")
    if (
        chapters_item["profile"] != basis
        or chapters_item["schema"] != ALBUM_PUBLICATION_CHAPTERS_SCHEMA
        or chapters_item["precision"] != "exact-integer-sample-positions"
        or cue_item["profile"] != basis
        or cue_item["timebase_frames_per_second"] != 75
        or cue_item["precision"] != "approximate-rounded-navigation-indexes"
    ):
        raise ExportError("Navigation inventory policy fields are inconsistent.")


def _verify_publication(root: Path) -> _VerifiedPublication:
    root = Path(os.path.abspath(os.fspath(root.expanduser())))
    root_identity = _directory_identity(root, label="Publication directory")
    manifest, manifest_receipt = load_album_publication_manifest(root / _MANIFEST_NAME)
    journal, journal_receipt, journal_identity = load_album_publication_journal(
        root / _JOURNAL_NAME
    )
    if journal["state"] != "verified-ready":
        raise ExportError("A final publication journal must be verified-ready.")
    if journal_identity != root_identity:
        raise ExportError("Final publication directory identity differs from its journal.")
    if journal["intended_output_name"] != root.name:
        raise ExportError("Final publication name differs from its ownership journal.")

    raw_inventory = manifest["inventory"]
    if not isinstance(raw_inventory, list) or len(raw_inventory) > _MAX_TREE_ENTRIES:
        raise ExportError("Publication inventory must be a bounded JSON array.")
    inventory = tuple(
        _validate_inventory_item(
            item,
            manifest_schema=str(manifest["schema"]),
        )
        for item in raw_inventory
    )
    if list(inventory) != sorted(inventory, key=lambda item: str(item["path"])):
        raise ExportError("Publication inventory is not in canonical path order.")
    inventory_by_path: dict[str, dict[str, Any]] = {}
    portable_paths: set[str] = set()
    for item in inventory:
        path_text = str(item["path"])
        key = portable_name_key(path_text)
        if path_text in inventory_by_path or key in portable_paths:
            raise ExportError("Publication inventory contains duplicate portable paths.")
        inventory_by_path[path_text] = item
        portable_paths.add(key)

    actual_files, _directories = _walk_tree(root)
    expected_files = set(inventory_by_path) | {_MANIFEST_NAME, _JOURNAL_NAME}
    if actual_files != expected_files:
        raise ExportError(
            "Publication tree differs from its exact inventory "
            f"(unexpected={sorted(actual_files - expected_files)}, "
            f"missing={sorted(expected_files - actual_files)})."
        )
    artifact_receipts: dict[str, FileReceipt] = {}
    for relative, item in inventory_by_path.items():
        artifact_path = root / Path(relative)
        receipt = capture_file_receipt(
            artifact_path,
            label=f"Publication artifact {relative}",
        )
        artifact_receipts[relative] = receipt
        if receipt.sha256 != item["sha256"] or receipt.size_bytes != item["size_bytes"]:
            raise ExportError(f"Publication artifact {relative!r} differs from inventory.")
        if artifact_path.suffix.casefold() in {".flac", ".m4a"}:
            verification = item["verification"]
            stored = verification["audio_attestation"]
            current = _audio_attestation(artifact_path)
            if current != stored:
                raise ExportError(f"Publication audio attestation changed for {relative!r}.")

    plan_path = root / "provenance" / "publication-plan.json"
    plan, raw_plan_sha256 = load_album_publication_plan_with_sha256(plan_path)
    plan_manifest = manifest["plan"]
    if not isinstance(plan_manifest, dict):
        raise ExportError("Manifest plan identity must be a JSON object.")
    _strict_keys(
        plan_manifest,
        {"raw_file_sha256", "body_sha256", "plan_sha256", "sibling_filename"},
        "Manifest plan identity",
    )
    if (
        raw_plan_sha256 != plan_manifest["raw_file_sha256"]
        or plan.body_sha256 != plan_manifest["body_sha256"]
        or plan.plan_sha256 != plan_manifest["plan_sha256"]
        or journal["plan_sha256"] != plan.plan_sha256
    ):
        raise ExportError("Manifest, journal, and stored plan identities disagree.")
    plan_filename = _strict_text(
        plan_manifest["sibling_filename"],
        "Plan filename",
    )
    if Path(plan_filename).name != plan_filename:
        raise ExportError("Manifest plan sibling filename is invalid.")

    album_path = root / "provenance" / "album-project.json"
    album, album_sha256 = load_album_project_with_sha256(album_path)
    album_manifest = manifest["album"]
    if not isinstance(album_manifest, dict):
        raise ExportError("Manifest album identity must be a JSON object.")
    _strict_keys(
        album_manifest,
        {"sha256", "size_bytes", "sibling_filename"},
        "Manifest album identity",
    )
    if album_sha256 != plan.album_sha256 or album_sha256 != album_manifest["sha256"]:
        raise ExportError("Stored album identity differs from plan or manifest.")
    album_receipt = capture_file_receipt(album_path, label="Stored album project")
    if album_receipt.size_bytes != album_manifest["size_bytes"]:
        raise ExportError("Stored album size differs from the manifest.")
    album_filename = _strict_text(
        album_manifest["sibling_filename"],
        "Album filename",
    )
    if Path(album_filename).name != album_filename:
        raise ExportError("Manifest album sibling filename is invalid.")

    if manifest["selected_profiles"] != list(plan.selected_profiles):
        raise ExportError("Manifest selected profiles differ from the immutable plan.")
    restoration_mode = (
        "reviewed"
        if any(
            side.restoration_render is not None or side.restoration_no_derivative is not None
            for side in plan.sides
        )
        else "none"
    )
    if manifest["restoration_mode"] != restoration_mode:
        raise ExportError("Manifest restoration mode differs from the immutable plan.")
    if manifest["processing_nodes"] != [node.to_dict() for node in plan.nodes]:
        raise ExportError("Manifest processing nodes differ from the immutable plan.")

    projects: dict[str, Project] = {}
    raw_sides = manifest["sides"]
    if not isinstance(raw_sides, list) or len(raw_sides) != len(plan.sides):
        raise ExportError("Manifest side ledger has the wrong side count.")
    if len(album.sides) != len(plan.sides):
        raise ExportError("Stored album and plan have different side counts.")
    for planned, album_side, raw_side in zip(
        plan.sides,
        album.sides,
        raw_sides,
        strict=True,
    ):
        if not isinstance(raw_side, dict):
            raise ExportError("Every manifest side must be a JSON object.")
        _strict_keys(
            raw_side,
            {
                "order",
                "label",
                "identity",
                "speed",
                "music_start_sample",
                "music_end_sample_exclusive",
                "restoration",
            },
            "Manifest side",
        )
        if (
            raw_side["order"] != planned.order
            or raw_side["label"] != planned.label
            or album_side.order != planned.order
            or album_side.label != planned.label
            or raw_side["identity"] != planned.current_identity.to_dict()
            or raw_side["speed"]
            != {
                "selected_speed_state_sha256": planned.selected_speed_state_sha256,
                "selected_effective_speed_factor": planned.selected_effective_speed_factor,
            }
        ):
            raise ExportError("Manifest, album, and plan side identities disagree.")
        project_path = (
            root / "provenance" / "sides" / f"{planned.order:02d}" / "project.groove.json"
        )
        project, project_sha256 = load_project_with_sha256(project_path)
        current_identity = {
            "project_revision": project.revision,
            "project_sha256": project_sha256,
            "editable_state_sha256": project.state_sha256,
            "source_sha256": project.source.sha256,
            "project_speed_state_sha256": project_speed_state(project).sha256,
        }
        if current_identity != planned.current_identity.to_dict():
            raise ExportError(f"Stored Side {planned.label} project identity is inconsistent.")
        if not project.tracks:
            raise ExportError(f"Stored Side {planned.label} project has no tracks.")
        if (
            raw_side["music_start_sample"] != project.tracks[0].start_sample
            or raw_side["music_end_sample_exclusive"] != project.tracks[-1].end_sample
        ):
            raise ExportError(f"Stored Side {planned.label} music range is inconsistent.")
        expected_restoration: dict[str, Any] | None = None
        if planned.restoration_render is not None:
            expected_restoration = {
                "outcome": "render",
                "manifest_sha256": planned.restoration_render.manifest_sha256,
                "audio_sha256": planned.restoration_render.audio_sha256,
            }
        elif planned.restoration_no_derivative is not None:
            expected_restoration = {
                "outcome": "clean",
                "manifest_sha256": planned.restoration_no_derivative.scan_sha256,
                "audio_sha256": None,
            }
        if raw_side["restoration"] != expected_restoration:
            raise ExportError(f"Stored Side {planned.label} restoration binding differs.")
        if expected_restoration is not None:
            manifest_name = (
                "render-manifest.json"
                if expected_restoration["outcome"] == "render"
                else "clean-scan.json"
            )
            restoration_receipt = capture_file_receipt(
                project_path.parent / "restoration" / manifest_name,
                label=f"Stored Side {planned.label} restoration manifest",
            )
            if restoration_receipt.sha256 != expected_restoration["manifest_sha256"]:
                raise ExportError(f"Stored Side {planned.label} restoration manifest differs.")
        projects[planned.label] = project

    observations = _tool_observations(manifest["tools"])
    settings = _settings_from_plan(plan)
    plan_sides = {side.label: side for side in plan.sides}
    for node in plan.nodes:
        if node.operation == "correct-speed-side":
            if node.side_label is None or node.side_label not in projects:
                raise ExportError("A stored speed node names an unknown side.")
            planned_side = plan_sides[node.side_label]
            validate_operation_tool_binding(
                node.operation,
                node.tool,
                settings,
                observations,
                source_sample_rate=projects[node.side_label].source.sample_rate,
                requested_speed_factor=planned_side.selected_effective_speed_factor,
                restoration_mode=restoration_mode,
            )
        else:
            validate_operation_tool_binding(
                node.operation,
                node.tool,
                settings,
                observations,
            )

    _validate_inventory_provenance(
        inventory,
        plan=plan,
        album=album,
        projects=projects,
        plan_raw_sha256=raw_plan_sha256,
        album_sha256=album_sha256,
    )
    archival_source_bindings = _validate_audio_role_semantics(
        inventory,
        plan=plan,
        album=album,
        projects=projects,
        manifest_schema=str(manifest["schema"]),
        archival_sources=manifest.get("archival_sources"),
    )

    _validate_navigation_from_provenance(
        root,
        inventory_by_path,
        manifest,
        album=album,
        projects=projects,
        archival_source_bindings=archival_source_bindings,
    )
    for relative, receipt in artifact_receipts.items():
        assert_file_receipt(
            root / Path(relative),
            receipt,
            label=f"Publication artifact {relative}",
        )
    assert_file_receipt(root / _MANIFEST_NAME, manifest_receipt, label="Manifest")
    assert_file_receipt(root / _JOURNAL_NAME, journal_receipt, label="Journal")
    final_files, _final_directories = _walk_tree(root)
    if final_files != expected_files:
        raise ExportError("Publication tree changed during verification.")
    if _directory_identity(root, label="Publication directory") != root_identity:
        raise ExportError("Publication directory changed during verification.")
    return _VerifiedPublication(
        root=root,
        manifest=manifest,
        journal=journal,
        inventory=inventory,
        manifest_receipt=manifest_receipt,
        journal_receipt=journal_receipt,
        plan=plan,
        projects=projects,
    )


def verify_album_publication(
    publication_directory: Path,
) -> AlbumPublicationVerificationReport:
    """Strictly verify one final publication without changing any path."""

    root = Path(os.path.abspath(os.fspath(publication_directory.expanduser())))
    try:
        verified = _verify_publication(root)
    except (GrooveSerpentError, OSError, TypeError, ValueError) as exc:
        mismatch = VerificationMismatch(
            code="verification_failed",
            path=None,
            expected="strict verified publication",
            current=None,
            message=str(exc),
        )
        return AlbumPublicationVerificationReport(
            publication_directory=str(root),
            ok=False,
            manifest_sha256=None,
            journal_sha256=None,
            artifact_count=0,
            mismatches=(mismatch,),
        )
    return AlbumPublicationVerificationReport(
        publication_directory=str(root),
        ok=True,
        manifest_sha256=verified.manifest_receipt.sha256,
        journal_sha256=verified.journal_receipt.sha256,
        artifact_count=len(verified.inventory),
        mismatches=(),
    )


def _append_mismatch(
    result: list[VerificationMismatch],
    *,
    code: str,
    path: str | None,
    expected: Any,
    current: Any,
    message: str,
) -> None:
    result.append(VerificationMismatch(code, path, expected, current, message))


def _compare_replay(
    original: _VerifiedPublication,
    replayed: _VerifiedPublication,
) -> tuple[VerificationMismatch, ...]:
    mismatches: list[VerificationMismatch] = []
    for key in (
        "schema",
        "plan",
        "album",
        "selected_profiles",
        "restoration_mode",
        "tools",
        "processing_nodes",
        "archival_sources",
        "sides",
    ):
        original_value = original.manifest.get(key)
        replayed_value = replayed.manifest.get(key)
        if original_value != replayed_value:
            _append_mismatch(
                mismatches,
                code="manifest_semantics_mismatch",
                path=key,
                expected=original_value,
                current=replayed_value,
                message=f"Replay manifest field {key!r} differs.",
            )
    original_items = {str(item["path"]): item for item in original.inventory}
    replay_items = {str(item["path"]): item for item in replayed.inventory}
    for path in sorted(set(original_items) | set(replay_items)):
        expected = original_items.get(path)
        current = replay_items.get(path)
        if expected is None or current is None:
            _append_mismatch(
                mismatches,
                code="inventory_path_mismatch",
                path=path,
                expected=expected,
                current=current,
                message="Replay inventory path is missing or unexpected.",
            )
            continue
        for field in set(expected) | set(current):
            if field in {"sha256", "size_bytes", "verification"}:
                continue
            if expected.get(field) != current.get(field):
                _append_mismatch(
                    mismatches,
                    code="inventory_semantics_mismatch",
                    path=path,
                    expected={field: expected.get(field)},
                    current={field: current.get(field)},
                    message=f"Replay inventory field {field!r} differs.",
                )
        deterministic = expected["role"] in {
            "full-capture-source",
            "input-snapshot",
            "album-artwork",
            "exact-chapters",
            "approximate-cue",
        }
        expected_verification = expected.get("verification")
        if (
            expected["role"] == "music-range-side"
            and isinstance(expected_verification, dict)
            and expected_verification.get("validated_restoration_render") is True
        ):
            deterministic = True
        if deterministic and (
            expected["sha256"] != current["sha256"]
            or expected["size_bytes"] != current["size_bytes"]
        ):
            _append_mismatch(
                mismatches,
                code="deterministic_bytes_mismatch",
                path=path,
                expected={"sha256": expected["sha256"], "size": expected["size_bytes"]},
                current={"sha256": current["sha256"], "size": current["size_bytes"]},
                message="Replay deterministic artifact bytes differ.",
            )
        if isinstance(expected_verification, dict):
            current_verification = current.get("verification")
            if not isinstance(current_verification, dict):
                _append_mismatch(
                    mismatches,
                    code="audio_verification_missing",
                    path=path,
                    expected=expected_verification,
                    current=current_verification,
                    message="Replay audio verification is missing.",
                )
                continue
            expected_audio = expected_verification.get("audio_attestation")
            current_audio = current_verification.get("audio_attestation")
            if isinstance(expected_audio, dict) and isinstance(current_audio, dict):
                for field in (
                    "codec_name",
                    "sample_rate",
                    "channels",
                    "bits_per_raw_sample",
                    "exact_sample_count",
                    "presentation_sample_count",
                    "decoded_pcm_sha256",
                    "semantic_tags",
                    "attached_picture_count",
                    "embedded_artwork_sha256",
                ):
                    if expected_audio.get(field) != current_audio.get(field):
                        _append_mismatch(
                            mismatches,
                            code="audio_identity_mismatch",
                            path=path,
                            expected={field: expected_audio.get(field)},
                            current={field: current_audio.get(field)},
                            message=f"Replay audio identity field {field!r} differs.",
                        )
    return tuple(mismatches)


def replay_album_publication(
    publication_directory: Path,
    replay_output_directory: Path,
    *,
    plan_path: Path,
    progress: Callable[[str], None] | None = None,
) -> AlbumPublicationReplayReport:
    """Execute an explicitly supplied original plan anew and compare without blessing."""

    original = _verify_publication(publication_directory)
    original_plan = Path(plan_path).expanduser()
    planned, raw_sha256 = load_album_publication_plan_with_sha256(original_plan)
    if (
        raw_sha256 != original.manifest["plan"]["raw_file_sha256"]
        or planned.plan_sha256 != original.plan.plan_sha256
    ):
        raise ExportError("Replay plan does not match the original immutable plan.")
    replay_root = Path(os.path.abspath(os.fspath(replay_output_directory.expanduser())))
    execute_album_publication_plan(
        original_plan,
        replay_root,
        progress=progress,
    )
    replayed = _verify_publication(replay_root)
    mismatches = _compare_replay(original, replayed)
    return AlbumPublicationReplayReport(
        original_directory=str(original.root),
        replay_directory=str(replayed.root),
        ok=not mismatches,
        mismatches=mismatches,
    )


def _orphan_name_kind(name: str) -> str | None:
    if _STAGE_NAME.fullmatch(name) is not None:
        return "partial"
    if _QUARANTINE_NAME.fullmatch(name) is not None:
        return "quarantine"
    return None


def _assert_directory_identity_unchanged(
    path: Path,
    expected: _DirectoryIdentity,
    *,
    label: str,
) -> None:
    if _directory_identity(path, label=label) != expected:
        raise ExportError(f"{label} was substituted during recovery.")


def _inventory_orphan(path: Path, kind: str) -> PublicationOrphan:
    try:
        identity = _directory_identity(path, label="Publication orphan")
        journal, journal_receipt, journal_identity = load_album_publication_journal(
            path / _JOURNAL_NAME
        )
        if journal_identity != identity:
            raise ExportError("Orphan directory identity differs from its journal.")
        operation_id = str(journal["operation_id"])
        if kind == "partial" and path.name != journal["original_stage_name"]:
            raise ExportError("Partial orphan name differs from its journal.")
        if kind == "quarantine" and _OPERATION_ID.fullmatch(operation_id) is None:
            raise ExportError("Quarantine journal operation ID is invalid.")
        files, _directories = _walk_tree(path)
        total_size = 0
        for relative in files:
            receipt = capture_file_receipt(
                path / Path(relative),
                label=f"Orphan artifact {relative}",
            )
            total_size += receipt.size_bytes
        return PublicationOrphan(
            path=str(path),
            kind=kind,
            owned=True,
            state=str(journal["state"]),
            plan_sha256=str(journal["plan_sha256"]),
            intended_output_name=str(journal["intended_output_name"]),
            journal_sha256=journal_receipt.sha256,
            directory_identity=RecoveryDirectoryIdentity.from_internal(identity),
            file_count=len(files),
            total_size_bytes=total_size,
            issue=None,
        )
    except (GrooveSerpentError, OSError, TypeError, ValueError) as exc:
        return PublicationOrphan(
            path=str(path),
            kind=kind,
            owned=False,
            state=None,
            plan_sha256=None,
            intended_output_name=None,
            journal_sha256=None,
            directory_identity=None,
            file_count=0,
            total_size_bytes=0,
            issue=str(exc),
        )


def inventory_album_publication_orphans(
    parent_directory: Path,
) -> PublicationOrphanInventory:
    """Inventory bounded direct-child owned stages without changing them."""

    parent = Path(os.path.abspath(os.fspath(parent_directory.expanduser())))
    parent_identity = _directory_identity(
        parent,
        label="Publication recovery parent",
    )
    orphans: list[PublicationOrphan] = []
    child_count = 0
    truncated = False
    try:
        entries = os.scandir(parent)
    except OSError as exc:
        raise ExportError("Publication recovery parent could not be read.") from exc
    with entries:
        _assert_directory_identity_unchanged(
            parent,
            parent_identity,
            label="Publication recovery parent",
        )
        for entry in entries:
            child_count += 1
            if child_count > _MAX_ORPHAN_CHILDREN:
                raise ExportError("Publication recovery parent has too many direct children.")
            kind = _orphan_name_kind(entry.name)
            if kind is None:
                continue
            if len(orphans) >= _MAX_ORPHANS:
                truncated = True
                continue
            _assert_directory_identity_unchanged(
                parent,
                parent_identity,
                label="Publication recovery parent",
            )
            orphans.append(_inventory_orphan(Path(entry.path), kind))
            _assert_directory_identity_unchanged(
                parent,
                parent_identity,
                label="Publication recovery parent",
            )
    _assert_directory_identity_unchanged(
        parent,
        parent_identity,
        label="Publication recovery parent",
    )
    return PublicationOrphanInventory(
        parent_directory=str(parent),
        orphans=tuple(sorted(orphans, key=lambda item: item.path)),
        truncated=truncated,
    )


def recover_album_publication_orphan(
    orphan_path: Path,
    *,
    expected_identity: RecoveryDirectoryIdentity,
    expected_journal_sha256: str,
    action: Literal["quarantine", "remove"],
) -> PublicationRecoveryReport:
    """Quarantine or remove one explicitly receipted owned direct-child orphan."""

    if action not in {"quarantine", "remove"}:
        raise ExportError("Recovery action must be 'quarantine' or 'remove'.")
    expected_sha = _strict_digest(
        expected_journal_sha256,
        "Expected orphan journal SHA-256",
    )
    path = Path(os.path.abspath(os.fspath(orphan_path.expanduser())))
    kind = _orphan_name_kind(path.name)
    if kind is None:
        raise ExportError("Recovery path is not an owned publication-orphan name.")
    parent = path.parent
    parent_identity = _directory_identity(
        parent,
        label="Publication recovery parent",
    )
    current = _directory_identity(path, label="Publication orphan")
    if current != expected_identity.internal():
        raise ExportError("Publication orphan identity differs from explicit approval.")
    journal, receipt, bound_identity = load_album_publication_journal(path / _JOURNAL_NAME)
    if receipt.sha256 != expected_sha or bound_identity != current:
        raise ExportError("Publication orphan journal differs from explicit approval.")
    if kind == "partial" and path.name != journal["original_stage_name"]:
        raise ExportError("Publication orphan name differs from its ownership journal.")
    _assert_directory_identity_unchanged(
        parent,
        parent_identity,
        label="Publication recovery parent",
    )
    if _directory_identity(path, label="Publication orphan") != current:
        raise ExportError("Publication orphan changed after explicit approval.")
    assert_file_receipt(path / _JOURNAL_NAME, receipt, label="Publication orphan journal")

    if action == "quarantine":
        destination = parent / (f".groove-serpent-album-cleanup-{uuid.uuid4().hex}.partial")
        _assert_directory_identity_unchanged(
            parent,
            parent_identity,
            label="Publication recovery parent",
        )
        _atomic_no_replace_directory(path, destination)
        _assert_directory_identity_unchanged(
            parent,
            parent_identity,
            label="Publication recovery parent",
        )
        if _directory_identity(destination, label="Quarantined publication orphan") != current:
            raise ExportError("Publication orphan changed during quarantine.")
        assert_file_receipt(
            destination / _JOURNAL_NAME,
            receipt,
            label="Quarantined publication orphan journal",
        )
        return PublicationRecoveryReport(
            action=action,
            original_path=str(path),
            resulting_path=str(destination),
            removed=False,
        )

    removal_path = path
    if kind == "partial":
        removal_path = parent / (f".groove-serpent-album-cleanup-{uuid.uuid4().hex}.partial")
        _assert_directory_identity_unchanged(
            parent,
            parent_identity,
            label="Publication recovery parent",
        )
        _atomic_no_replace_directory(path, removal_path)
        _assert_directory_identity_unchanged(
            parent,
            parent_identity,
            label="Publication recovery parent",
        )
        if _directory_identity(removal_path, label="Quarantined publication orphan") != current:
            raise ExportError("Publication orphan changed during removal quarantine.")
    _assert_directory_identity_unchanged(
        parent,
        parent_identity,
        label="Publication recovery parent",
    )
    if _directory_identity(removal_path, label="Publication orphan for removal") != current:
        raise ExportError("Publication orphan changed before removal.")
    assert_file_receipt(
        removal_path / _JOURNAL_NAME,
        receipt,
        label="Publication orphan journal before removal",
    )
    _remove_owned_stage(removal_path, current)
    _assert_directory_identity_unchanged(
        parent,
        parent_identity,
        label="Publication recovery parent",
    )
    return PublicationRecoveryReport(
        action=action,
        original_path=str(path),
        resulting_path=None,
        removed=True,
    )


__all__ = [
    "AlbumPublicationReplayReport",
    "AlbumPublicationVerificationReport",
    "PublicationOrphan",
    "PublicationOrphanInventory",
    "PublicationRecoveryReport",
    "RecoveryDirectoryIdentity",
    "VerificationMismatch",
    "inventory_album_publication_orphans",
    "load_album_publication_journal",
    "load_album_publication_manifest",
    "recover_album_publication_orphan",
    "replay_album_publication",
    "verify_album_publication",
]
