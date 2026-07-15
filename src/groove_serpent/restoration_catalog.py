"""Read-only discovery and validation for persisted restoration artifacts.

The review server creates restoration artifacts in one dedicated, flat
workspace.  This module rebuilds the in-memory view after a restart without
trusting filenames, JSON, dependency bindings, or referenced audio bytes.
Discovery never creates, repairs, removes, or rewrites anything.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Mapping, cast

from .errors import GrooveSerpentError, ProjectValidationError
from .models import Project, resolve_source_path
from .project_io import load_project_with_sha256
from .restoration import MAX_REPAIR_SAMPLES
from .restoration_workflow import (
    MAX_PREVIEW_CANDIDATES,
    PREVIEW_SCHEMA,
    RECIPE_SCHEMA,
    REMOVED_SIGNAL_GAIN,
    RENDER_SCHEMA,
    REPAIR_BACKEND,
    SCAN_SCHEMA,
    _detector_manifest,
    _restoration_coverage,
    _validate_recipe_payload,
    _validated_scan_candidates,
)


ArtifactKind = Literal["scan", "recipe", "preview", "render"]

MAX_WORKSPACE_ENTRIES = 4_096
MAX_BUNDLE_ENTRIES = 16
MAX_ARTIFACTS = 1_024
MAX_MANIFEST_BYTES = 50 * 1024 * 1024
MAX_REFERENCED_FILE_BYTES = (1 << 63) - 1
_READ_CHUNK_BYTES = 1024 * 1024
_REPARSE_POINT = 0x400
_KIND_ORDER: dict[ArtifactKind, int] = {
    "scan": 0,
    "recipe": 1,
    "preview": 2,
    "render": 3,
}
_FILE_NAME = re.compile(r"^(scan|recipe)-[0-9a-f]{32}\.json$")
_BUNDLE_NAME = re.compile(r"^(preview|render)-[0-9a-f]{32}$")
_SCHEMAS: dict[ArtifactKind, str] = {
    "scan": SCAN_SCHEMA,
    "recipe": RECIPE_SCHEMA,
    "preview": PREVIEW_SCHEMA,
    "render": RENDER_SCHEMA,
}


@dataclass(frozen=True, slots=True)
class RestorationDependency:
    """One exact manifest dependency resolved inside the same workspace."""

    kind: ArtifactKind
    name: str
    sha256: str
    artifact_id: str


@dataclass(frozen=True, slots=True)
class RestorationFile:
    """One referenced, content-verified output file."""

    role: str
    name: str
    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class RestorationArtifact:
    """A structurally valid artifact, either current or explicitly stale."""

    artifact_id: str
    kind: ArtifactKind
    manifest_path: Path
    manifest_sha256: str
    created_at: str
    created_at_utc: str
    payload: dict[str, Any] = field(compare=False, repr=False)
    dependencies: tuple[RestorationDependency, ...] = ()
    files: tuple[RestorationFile, ...] = ()
    stale_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RestorationCatalogIssue:
    """A rejected workspace entry with a stable machine-readable reason."""

    path: Path
    kind: ArtifactKind | None
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class RestorationSelection:
    """Newest coherent current chain, matching review-server reset semantics."""

    scan: RestorationArtifact | None
    recipe: RestorationArtifact | None
    preview: RestorationArtifact | None
    render: RestorationArtifact | None


@dataclass(frozen=True, slots=True)
class RestorationCatalog:
    """Deterministic current, stale, and invalid restoration inventory."""

    workspace: Path
    project_path: Path
    project_sha256: str
    source_path: Path
    source_sha256: str
    artifacts: tuple[RestorationArtifact, ...]
    stale: tuple[RestorationArtifact, ...]
    invalid: tuple[RestorationCatalogIssue, ...]

    def latest(self, kind: ArtifactKind) -> RestorationArtifact | None:
        """Return the deterministic newest current artifact of ``kind``."""

        matches = [item for item in self.artifacts if item.kind == kind]
        return max(matches, key=_artifact_sort_key, default=None)

    def by_id(self, artifact_id: str) -> RestorationArtifact | None:
        """Return one current artifact by stable content-derived identifier."""

        return next(
            (item for item in self.artifacts if item.artifact_id == artifact_id),
            None,
        )

    def latest_chain(self) -> RestorationSelection:
        """Return the newest coherent current scan and its dependent work."""

        scan = self.latest("scan")
        if scan is None:
            return RestorationSelection(None, None, None, None)

        def newest(
            kind: ArtifactKind,
            dependency_ids: tuple[str, ...],
        ) -> RestorationArtifact | None:
            candidates = [
                item
                for item in self.artifacts
                if item.kind == kind
                and tuple(dependency.artifact_id for dependency in item.dependencies)
                == dependency_ids
            ]
            return max(candidates, key=_artifact_sort_key, default=None)

        recipe = newest("recipe", (scan.artifact_id,))
        preview = newest("preview", (scan.artifact_id,))
        render = (
            newest("render", (scan.artifact_id, recipe.artifact_id)) if recipe is not None else None
        )
        return RestorationSelection(scan, recipe, preview, render)


@dataclass(slots=True)
class _Provisional:
    kind: ArtifactKind
    manifest_path: Path
    manifest_sha256: str
    artifact_id: str
    created_at: str
    created_at_utc: str
    payload: dict[str, Any]
    files: tuple[RestorationFile, ...]
    dependency_specs: tuple[tuple[ArtifactKind, str, str], ...]
    dependencies: tuple[RestorationDependency, ...] = ()
    stale_reasons: tuple[str, ...] = ()


class _InvalidArtifact(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _artifact_sort_key(
    artifact: RestorationArtifact,
) -> tuple[int, str, str, str]:
    return (
        _KIND_ORDER[artifact.kind],
        artifact.created_at_utc,
        artifact.manifest_path.as_posix(),
        artifact.manifest_sha256,
    )


def _invalid(code: str, message: str) -> _InvalidArtifact:
    return _InvalidArtifact(code, message)


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _safe_lstat(path: Path, *, directory: bool) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise _invalid("unreadable_path", "The artifact path could not be inspected.") from exc
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
        raise _invalid(
            "unsafe_reparse_path",
            "Symlinks, junctions, and other reparse points are not restoration artifacts.",
        )
    expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not expected:
        expected_label = "directory" if directory else "regular file"
        raise _invalid("unsafe_file_type", f"The artifact must be a {expected_label}.")
    return metadata


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    return all(getattr(left, key, None) == getattr(right, key, None) for key in fields)


def _hash_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
) -> tuple[str, int]:
    before = _safe_lstat(path, directory=False)
    if before.st_size < 0 or before.st_size > maximum_bytes:
        raise _invalid("file_size_limit", "The artifact file exceeds its safe size limit.")
    digest = hashlib.sha256()
    observed = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                observed += len(chunk)
                if observed > maximum_bytes:
                    raise _invalid(
                        "file_size_limit",
                        "The artifact file grew beyond its safe size limit.",
                    )
                digest.update(chunk)
    except _InvalidArtifact:
        raise
    except OSError as exc:
        raise _invalid("unreadable_file", "The artifact file could not be read.") from exc
    after = _safe_lstat(path, directory=False)
    if observed != before.st_size or not _same_file_snapshot(before, after):
        raise _invalid("file_changed", "The artifact file changed while it was verified.")
    return digest.hexdigest(), observed


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _finite_float(value: str) -> float:
    rendered = float(value)
    if not math.isfinite(rendered):
        raise ValueError(f"Invalid JSON number: {value}")
    return rendered


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def _load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    before = _safe_lstat(path, directory=False)
    if before.st_size <= 0 or before.st_size > MAX_MANIFEST_BYTES:
        raise _invalid(
            "manifest_size_limit",
            "A restoration manifest must contain 1 byte to 50 MB.",
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _invalid(
            "unreadable_manifest", "The restoration manifest could not be read."
        ) from exc
    after = _safe_lstat(path, directory=False)
    if len(raw) != before.st_size or not _same_file_snapshot(before, after):
        raise _invalid("manifest_changed", "The restoration manifest changed while read.")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _invalid(
            "invalid_json",
            "The restoration manifest is not strict, finite, duplicate-free JSON.",
        ) from exc
    if type(payload) is not dict:
        raise _invalid("invalid_schema", "The restoration manifest root must be an object.")
    return cast(dict[str, Any], payload), hashlib.sha256(raw).hexdigest()


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise _invalid(
            "invalid_schema",
            f"{label} must contain exactly: {', '.join(sorted(keys))}.",
        )
    return cast(dict[str, Any], value)


def _text(value: Any, label: str, *, maximum: int = 4_096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _invalid("invalid_schema", f"{label} must be bounded non-empty text.")
    return value


def _digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise _invalid("invalid_schema", f"{label} must be a lowercase SHA-256 digest.")
    return value


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = (1 << 63) - 1,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid("invalid_schema", f"{label} is outside the supported integer range.")
    return value


def _number(value: Any, label: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid("invalid_schema", f"{label} must be a finite number.")
    rendered = float(value)
    if not math.isfinite(rendered) or not minimum <= rendered <= maximum:
        raise _invalid("invalid_schema", f"{label} is outside the supported range.")
    return rendered


def _true(value: Any, label: str) -> None:
    if value is not True:
        raise _invalid("invalid_schema", f"{label} must be true.")


def _basename(value: Any, label: str, *, expected: str | None = None) -> str:
    rendered = _text(value, label, maximum=255)
    if (
        rendered in {".", ".."}
        or Path(rendered).name != rendered
        or "/" in rendered
        or "\\" in rendered
        or "\x00" in rendered
    ):
        raise _invalid("unsafe_manifest_path", f"{label} must be one safe basename.")
    if expected is not None and rendered != expected:
        raise _invalid("unsafe_manifest_path", f"{label} must be {expected}.")
    return rendered


def _created_at(payload: Mapping[str, Any]) -> tuple[str, str]:
    value = _text(payload.get("created_at"), "Artifact created_at", maximum=64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _invalid("invalid_schema", "Artifact created_at must be ISO-8601 text.") from exc
    if parsed.tzinfo is None:
        raise _invalid("invalid_schema", "Artifact created_at must include a timezone.")
    normalized = parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return value, normalized


def _provenance(payload: Mapping[str, Any]) -> tuple[str, str]:
    created_at, created_at_utc = _created_at(payload)
    _text(payload.get("app_version"), "Artifact app_version", maximum=200)
    return created_at, created_at_utc


def _binding(
    value: Any,
    label: str,
    *,
    extra_keys: set[str] | None = None,
) -> tuple[dict[str, Any], str, str]:
    keys = {"path", "sha256"} | (extra_keys or set())
    binding = _exact(value, keys, label)
    name = _basename(binding["path"], f"{label} path")
    digest = _digest(binding["sha256"], f"{label} SHA-256")
    return binding, name, digest


def _validate_coverage_shape(value: Any) -> dict[str, Any]:
    coverage = _exact(
        value,
        {
            "music_start_frame",
            "music_end_frame_exclusive",
            "music_frame_count",
            "scanned_music_frames",
            "scanned_music_percent",
            "scan_range_covers_music",
            "candidate_scan_truncated",
            "detected_candidates",
            "retained_candidates",
            "unretained_detections",
            "unreviewed_regions",
            "restoration_status",
        },
        "Restoration coverage",
    )
    start = _integer(coverage["music_start_frame"], "Coverage music start")
    end = _integer(coverage["music_end_frame_exclusive"], "Coverage music end", minimum=1)
    count = _integer(coverage["music_frame_count"], "Coverage music frame count", minimum=1)
    scanned = _integer(coverage["scanned_music_frames"], "Coverage scanned frames")
    if end <= start or count != end - start or scanned > count:
        raise _invalid("invalid_schema", "Restoration coverage frame counts are inconsistent.")
    percent = _number(
        coverage["scanned_music_percent"],
        "Coverage scanned percent",
        minimum=0.0,
        maximum=100.0,
    )
    if abs(percent - scanned * 100.0 / count) > 1e-9:
        raise _invalid("invalid_schema", "Restoration coverage percentage is inconsistent.")
    for key in ("scan_range_covers_music", "candidate_scan_truncated"):
        if type(coverage[key]) is not bool:
            raise _invalid("invalid_schema", f"Coverage {key} must be boolean.")
    detected = _integer(coverage["detected_candidates"], "Coverage detections")
    retained = _integer(coverage["retained_candidates"], "Coverage retained candidates")
    omitted = _integer(coverage["unretained_detections"], "Coverage omitted candidates")
    if retained > detected or omitted != detected - retained:
        raise _invalid("invalid_schema", "Restoration coverage candidate counts disagree.")
    regions = coverage["unreviewed_regions"]
    if type(regions) is not list or len(regions) > 2:
        raise _invalid("invalid_schema", "Coverage unreviewed regions are invalid.")
    previous_end = -1
    for raw in regions:
        region = _exact(
            raw,
            {"start_frame", "end_frame_exclusive"},
            "Coverage unreviewed region",
        )
        region_start = _integer(region["start_frame"], "Unreviewed region start")
        region_end = _integer(region["end_frame_exclusive"], "Unreviewed region end")
        if region_end <= region_start or region_start < previous_end:
            raise _invalid("invalid_schema", "Coverage unreviewed regions are inconsistent.")
        previous_end = region_end
    status = coverage["restoration_status"]
    if status not in {"complete", "partial", "exploratory"}:
        raise _invalid("invalid_schema", "Restoration coverage status is invalid.")
    if status == "complete" and (
        scanned != count
        or coverage["scan_range_covers_music"] is not True
        or coverage["candidate_scan_truncated"] is not False
        or regions
    ):
        raise _invalid("invalid_schema", "Complete restoration coverage is inconsistent.")
    return coverage


def _validate_scan(
    payload: dict[str, Any],
    project: Project,
    current_project_sha256: str,
) -> None:
    _exact(
        payload,
        {
            "schema",
            "created_at",
            "app_version",
            "project",
            "source",
            "decoder",
            "detector",
            "scan",
            "candidates",
            "summary",
            "coverage",
        },
        "Click scan",
    )
    _provenance(payload)
    project_binding, _project_name, project_sha = _binding(payload["project"], "Scan project")
    source = _exact(
        payload["source"],
        {
            "path",
            "sha256",
            "size_bytes",
            "sample_rate",
            "channels",
            "bits_per_raw_sample",
            "sample_count",
        },
        "Scan source",
    )
    _basename(source["path"], "Scan source path")
    source_sha = _digest(source["sha256"], "Scan source SHA-256")
    source_size = _integer(source["size_bytes"], "Scan source size", minimum=1)
    sample_rate = _integer(source["sample_rate"], "Scan source sample rate", minimum=1)
    channels = _integer(source["channels"], "Scan source channels", minimum=1, maximum=64)
    bits = _integer(source["bits_per_raw_sample"], "Scan source bit depth", minimum=1)
    if bits not in {16, 24}:
        raise _invalid("invalid_schema", "Scan source bit depth is unsupported.")
    sample_count = _integer(source["sample_count"], "Scan source sample count", minimum=1)
    decoder = _exact(
        payload["decoder"],
        {
            "ffmpeg",
            "canonical_pcm",
            "bytes_per_frame",
            "immutable_source_snapshot",
            "source_snapshot_sha256",
        },
        "Scan decoder",
    )
    _text(decoder["ffmpeg"], "Scan FFmpeg version", maximum=4_096)
    expected_pcm = "s16le-interleaved" if bits == 16 else "s32le-interleaved"
    if decoder["canonical_pcm"] != expected_pcm:
        raise _invalid("invalid_schema", "Scan canonical PCM format is inconsistent.")
    expected_bytes = channels * (2 if bits == 16 else 4)
    if decoder["bytes_per_frame"] != expected_bytes:
        raise _invalid("invalid_schema", "Scan PCM frame size is inconsistent.")
    _true(decoder["immutable_source_snapshot"], "Scan immutable snapshot proof")
    if _digest(decoder["source_snapshot_sha256"], "Scan snapshot SHA-256") != source_sha:
        raise _invalid("invalid_schema", "Scan snapshot and source SHA-256 disagree.")
    if payload["detector"] != _detector_manifest():
        raise _invalid("invalid_schema", "Scan detector parameters are unsupported.")
    scan_range = _exact(
        payload["scan"],
        {"start_frame", "end_frame_exclusive", "start_seconds", "end_seconds"},
        "Scan range",
    )
    scan_start = _integer(scan_range["start_frame"], "Scan start")
    scan_end = _integer(scan_range["end_frame_exclusive"], "Scan end", minimum=1)
    if scan_end <= scan_start or scan_end > sample_count:
        raise _invalid("invalid_schema", "Scan frame range is inconsistent.")
    if scan_end - scan_start < 256:
        raise _invalid("invalid_schema", "Scan frame range is shorter than 256 frames.")
    start_seconds = _number(
        scan_range["start_seconds"], "Scan start seconds", minimum=0.0, maximum=float(sample_count)
    )
    end_seconds = _number(
        scan_range["end_seconds"], "Scan end seconds", minimum=0.0, maximum=float(sample_count)
    )
    if (
        abs(start_seconds - scan_start / sample_rate) > 1e-9
        or abs(end_seconds - scan_end / sample_rate) > 1e-9
    ):
        raise _invalid("invalid_schema", "Scan sample and time ranges disagree.")
    embedded_source = SimpleNamespace(
        sha256=source_sha,
        size_bytes=source_size,
        sample_rate=sample_rate,
        channels=channels,
        bits_per_raw_sample=bits,
        sample_count=sample_count,
    )
    try:
        candidates = _validated_scan_candidates(
            payload,
            project_sha256=project_sha,
            source=embedded_source,
        )
    except GrooveSerpentError as exc:
        raise _invalid("invalid_schema", str(exc)) from exc
    summary = _exact(
        payload["summary"],
        {"detected", "retained", "truncated", "clipped", "impulse", "repairable"},
        "Scan summary",
    )
    detected = _integer(summary["detected"], "Scan detected count")
    retained = _integer(summary["retained"], "Scan retained count")
    if type(summary["truncated"]) is not bool or summary["truncated"] != (detected > retained):
        raise _invalid("invalid_schema", "Scan truncation summary is inconsistent.")
    expected_summary = {
        "retained": len(candidates),
        "clipped": sum(item["type"] == "clipped" for item in candidates.values()),
        "impulse": sum(item["type"] == "impulse" for item in candidates.values()),
        "repairable": sum(bool(item["repairable"]) for item in candidates.values()),
    }
    if retained > detected or any(summary[key] != value for key, value in expected_summary.items()):
        raise _invalid("invalid_schema", "Scan candidate summary is inconsistent.")
    coverage = _validate_coverage_shape(payload["coverage"])
    if (
        coverage["detected_candidates"] != detected
        or coverage["retained_candidates"] != retained
        or coverage["candidate_scan_truncated"] != summary["truncated"]
    ):
        raise _invalid("invalid_schema", "Scan coverage and summary disagree.")
    music_start = cast(int, coverage["music_start_frame"])
    music_end = cast(int, coverage["music_end_frame_exclusive"])
    covered_start = max(scan_start, music_start)
    covered_end = min(scan_end, music_end)
    expected_scanned = max(0, covered_end - covered_start)
    expected_regions: list[dict[str, int]] = []
    if scan_start > music_start:
        region_end = min(scan_start, music_end)
        if region_end > music_start:
            expected_regions.append({"start_frame": music_start, "end_frame_exclusive": region_end})
    if scan_end < music_end:
        region_start = max(scan_end, music_start)
        if music_end > region_start:
            expected_regions.append({"start_frame": region_start, "end_frame_exclusive": music_end})
    covers_music = scan_start <= music_start and scan_end >= music_end
    complete = covers_music and summary["truncated"] is False
    expected_status = "complete" if complete else ("partial" if expected_scanned else "exploratory")
    if (
        coverage["scanned_music_frames"] != expected_scanned
        or coverage["scan_range_covers_music"] != covers_music
        or coverage["unreviewed_regions"] != expected_regions
        or coverage["restoration_status"] != expected_status
    ):
        raise _invalid("invalid_schema", "Scan range and coverage ledger disagree.")
    # A current project permits the strongest possible coverage validation.  A
    # stale project's old track geometry is unavailable, so its internally
    # consistent ledger remains inspectable but never actionable.
    if project_binding["sha256"] == current_project_sha256:
        try:
            _restoration_coverage(payload, project)
        except GrooveSerpentError as exc:
            raise _invalid("invalid_schema", str(exc)) from exc


def _validate_recipe_shape(payload: dict[str, Any]) -> None:
    allowed = {
        "schema",
        "created_at",
        "app_version",
        "project",
        "source",
        "scan",
        "backend",
        "decisions",
        "summary",
        "coverage",
    }
    _exact(payload, allowed, "Restoration recipe")
    _provenance(payload)
    _binding(payload["project"], "Recipe project")
    _binding(payload["source"], "Recipe source")
    _binding(payload["scan"], "Recipe scan")
    backend = _exact(payload["backend"], {"name", "maximum_repair_frames"}, "Recipe backend")
    if backend != {"name": REPAIR_BACKEND, "maximum_repair_frames": MAX_REPAIR_SAMPLES}:
        raise _invalid("invalid_schema", "Recipe backend is unsupported.")
    if type(payload["decisions"]) is not list or len(payload["decisions"]) > 10_000:
        raise _invalid("invalid_schema", "Recipe decisions must be a bounded array.")
    _exact(
        payload["summary"],
        {"candidates", "approved", "rejected", "protected"},
        "Recipe summary",
    )
    _validate_coverage_shape(payload["coverage"])


def _verify_bundle_contents(bundle: Path, expected: set[str]) -> None:
    try:
        entries: list[os.DirEntry[str]] = []
        with os.scandir(bundle) as iterator:
            for entry in iterator:
                entries.append(entry)
                if len(entries) > MAX_BUNDLE_ENTRIES:
                    raise _invalid(
                        "bundle_entry_limit", "A restoration bundle has too many entries."
                    )
    except _InvalidArtifact:
        raise
    except OSError as exc:
        raise _invalid("unreadable_bundle", "The restoration bundle could not be listed.") from exc
    names = {entry.name for entry in entries}
    if names != expected:
        raise _invalid(
            "invalid_bundle", "The restoration bundle contains unexpected or missing files."
        )
    for entry in entries:
        _safe_lstat(bundle / entry.name, directory=False)


def _validated_files(
    bundle: Path,
    value: Any,
    expected_names: Mapping[str, str],
    *,
    render: bool,
) -> tuple[RestorationFile, ...]:
    files = _exact(value, set(expected_names), "Restoration output files")
    result: list[RestorationFile] = []
    for role, expected_name in expected_names.items():
        keys = {"path", "sha256"}
        if render:
            keys |= {"sample_count", "sample_rate", "channels", "bits_per_raw_sample"}
        binding = _exact(files[role], keys, f"Restoration {role} output")
        name = _basename(binding["path"], f"Restoration {role} output path", expected=expected_name)
        expected_sha = _digest(binding["sha256"], f"Restoration {role} output SHA-256")
        path = bundle / name
        observed_sha, size = _hash_regular_file(path, maximum_bytes=MAX_REFERENCED_FILE_BYTES)
        if observed_sha != expected_sha:
            raise _invalid(
                "output_hash_mismatch",
                f"The referenced {role} output bytes do not match the manifest.",
            )
        result.append(RestorationFile(role, name, path, observed_sha, size))
    return tuple(result)


def _validate_preview_shape(payload: dict[str, Any], bundle: Path) -> tuple[RestorationFile, ...]:
    _exact(
        payload,
        {
            "schema",
            "created_at",
            "app_version",
            "source",
            "scan",
            "candidates",
            "context",
            "backend",
            "files",
            "audition",
            "metrics",
            "proof",
            "approval",
        },
        "Click preview",
    )
    _provenance(payload)
    source = _exact(
        payload["source"],
        {"path", "sha256", "sample_rate", "channels", "bits_per_raw_sample"},
        "Preview source",
    )
    _basename(source["path"], "Preview source path")
    source_sha = _digest(source["sha256"], "Preview source SHA-256")
    _integer(source["sample_rate"], "Preview sample rate", minimum=1)
    _integer(source["channels"], "Preview channels", minimum=1, maximum=64)
    if _integer(source["bits_per_raw_sample"], "Preview bit depth", minimum=1) not in {16, 24}:
        raise _invalid("invalid_schema", "Preview bit depth is unsupported.")
    _binding(payload["scan"], "Preview scan")
    candidates = payload["candidates"]
    if type(candidates) is not list or not 1 <= len(candidates) <= MAX_PREVIEW_CANDIDATES:
        raise _invalid("invalid_schema", "Preview candidates must be a bounded non-empty array.")
    context = _exact(
        payload["context"],
        {
            "start_frame",
            "end_frame_exclusive",
            "repair_start_in_preview",
            "repair_end_in_preview_exclusive",
            "repair_windows",
        },
        "Preview context",
    )
    context_start = _integer(context["start_frame"], "Preview context start")
    context_end = _integer(context["end_frame_exclusive"], "Preview context end", minimum=1)
    local_start = _integer(context["repair_start_in_preview"], "Preview repair start")
    local_end = _integer(
        context["repair_end_in_preview_exclusive"], "Preview repair end", minimum=1
    )
    if (
        context_end <= context_start
        or local_end <= local_start
        or local_end > context_end - context_start
    ):
        raise _invalid("invalid_schema", "Preview context ranges are inconsistent.")
    windows = context["repair_windows"]
    if type(windows) is not list or len(windows) != len(candidates):
        raise _invalid("invalid_schema", "Preview repair windows are inconsistent.")
    backend = _exact(
        payload["backend"],
        {
            "name",
            "maximum_repair_frames",
            "audacity_used",
            "immutable_source_snapshot",
            "source_snapshot_sha256",
        },
        "Preview backend",
    )
    if (
        backend["name"] != REPAIR_BACKEND
        or backend["maximum_repair_frames"] != MAX_REPAIR_SAMPLES
        or backend["audacity_used"] is not False
        or backend["immutable_source_snapshot"] is not True
        or _digest(backend["source_snapshot_sha256"], "Preview snapshot SHA-256") != source_sha
    ):
        raise _invalid("invalid_schema", "Preview backend proof is inconsistent.")
    audition = _exact(
        payload["audition"],
        {
            "before_linear_gain",
            "proposed_linear_gain",
            "removed_linear_gain",
            "removed_gain_db",
            "definition",
            "matched_original_level",
        },
        "Preview audition",
    )
    gains = (
        _number(audition["before_linear_gain"], "Before gain", minimum=0.0, maximum=1_000.0),
        _number(audition["proposed_linear_gain"], "Proposed gain", minimum=0.0, maximum=1_000.0),
        _number(audition["removed_linear_gain"], "Removed gain", minimum=0.0, maximum=1_000.0),
    )
    removed_db = _number(
        audition["removed_gain_db"], "Removed gain dB", minimum=-1_000.0, maximum=1_000.0
    )
    if (
        gains != (1.0, 1.0, REMOVED_SIGNAL_GAIN)
        or abs(removed_db - 20.0 * math.log10(REMOVED_SIGNAL_GAIN)) > 1e-9
        or audition["definition"] != "removed = (before - proposed) * removed_linear_gain"
        or audition["matched_original_level"] is not True
    ):
        raise _invalid("invalid_schema", "Preview audition contract is inconsistent.")
    metrics = _exact(
        payload["metrics"],
        {
            "before",
            "proposed",
            "changed_scalar_samples",
            "removed_peak_absolute_sample",
            "removed_clipped_scalar_samples",
        },
        "Preview metrics",
    )
    for role in ("before", "proposed"):
        value = _exact(
            metrics[role],
            {"approved_peak_absolute_sample", "approved_local_curvature_rms", "window_boundaries"},
            f"Preview {role} metrics",
        )
        _integer(value["approved_peak_absolute_sample"], f"Preview {role} peak")
        _number(
            value["approved_local_curvature_rms"],
            f"Preview {role} curvature",
            minimum=0.0,
            maximum=float(1 << 63),
        )
        boundaries = value["window_boundaries"]
        if type(boundaries) is not list or len(boundaries) != len(candidates):
            raise _invalid("invalid_schema", "Preview boundary metrics are inconsistent.")
        for raw_boundary in boundaries:
            boundary = _exact(
                raw_boundary,
                {"candidate_id", "channels", "left_jump", "right_jump"},
                f"Preview {role} boundary metric",
            )
            _text(
                boundary["candidate_id"],
                f"Preview {role} boundary candidate ID",
                maximum=160,
            )
            channels = boundary["channels"]
            left = boundary["left_jump"]
            right = boundary["right_jump"]
            if (
                type(channels) is not list
                or not channels
                or type(left) is not list
                or type(right) is not list
                or len(left) != len(channels)
                or len(right) != len(channels)
            ):
                raise _invalid(
                    "invalid_schema",
                    "Preview boundary channels and jumps are inconsistent.",
                )
            for channel in channels:
                _integer(channel, f"Preview {role} boundary channel", maximum=63)
            for jump in [*left, *right]:
                _integer(jump, f"Preview {role} boundary jump")
    _integer(metrics["changed_scalar_samples"], "Preview changed samples", minimum=1)
    _integer(metrics["removed_peak_absolute_sample"], "Preview removed peak")
    _integer(metrics["removed_clipped_scalar_samples"], "Preview removed clipping")
    proof = _exact(
        payload["proof"],
        {
            "source_unchanged",
            "immutable_source_snapshot",
            "lossless_preview_round_trip",
            "outside_approved_windows_and_channels_identical",
            "frame_count_equal",
            "format_equal",
            "removed_signal_matches_declared_difference",
        },
        "Preview proof",
    )
    for key, value in proof.items():
        _true(value, f"Preview proof {key}")
    approval = _exact(payload["approval"], {"status", "instruction"}, "Preview approval")
    if approval["status"] != "pending":
        raise _invalid("invalid_schema", "Rediscovered previews must remain pending audition.")
    _text(approval["instruction"], "Preview approval instruction", maximum=4_096)
    files = _validated_files(
        bundle,
        payload["files"],
        {"before": "before.flac", "proposed": "proposed.flac", "removed": "removed.flac"},
        render=False,
    )
    _verify_bundle_contents(bundle, {"preview.json", *(item.name for item in files)})
    return files


def _validate_render_shape(payload: dict[str, Any], bundle: Path) -> tuple[RestorationFile, ...]:
    _exact(
        payload,
        {
            "schema",
            "created_at",
            "app_version",
            "project",
            "source",
            "scan",
            "recipe",
            "music_range",
            "coverage",
            "backend",
            "repairs",
            "protected",
            "files",
            "pcm_proof",
            "proof",
        },
        "Restoration render",
    )
    _provenance(payload)
    _binding(payload["project"], "Render project")
    _binding(payload["source"], "Render source")
    _binding(payload["scan"], "Render scan")
    recipe, _name, _sha = _binding(payload["recipe"], "Render recipe", extra_keys={"schema"})
    if recipe["schema"] != RECIPE_SCHEMA:
        raise _invalid("invalid_schema", "Render recipe schema binding is invalid.")
    music = _exact(
        payload["music_range"],
        {"start_frame", "end_frame_exclusive", "sample_count"},
        "Render music range",
    )
    start = _integer(music["start_frame"], "Render music start")
    end = _integer(music["end_frame_exclusive"], "Render music end", minimum=1)
    count = _integer(music["sample_count"], "Render music sample count", minimum=1)
    if end <= start or count != end - start:
        raise _invalid("invalid_schema", "Render music range is inconsistent.")
    coverage = _validate_coverage_shape(payload["coverage"])
    if coverage["restoration_status"] != "complete":
        raise _invalid("invalid_schema", "A full render requires complete restoration coverage.")
    backend = _exact(
        payload["backend"],
        {
            "name",
            "maximum_repair_frames",
            "streaming_source_decode",
            "audacity_used",
            "immutable_source_snapshot",
            "source_snapshot_sha256",
        },
        "Render backend",
    )
    source_binding = cast(dict[str, Any], payload["source"])
    if (
        backend["name"] != REPAIR_BACKEND
        or backend["maximum_repair_frames"] != MAX_REPAIR_SAMPLES
        or backend["streaming_source_decode"] is not True
        or backend["audacity_used"] is not False
        or backend["immutable_source_snapshot"] is not True
        or _digest(backend["source_snapshot_sha256"], "Render snapshot SHA-256")
        != source_binding["sha256"]
    ):
        raise _invalid("invalid_schema", "Render backend proof is inconsistent.")
    if type(payload["repairs"]) is not list or not payload["repairs"]:
        raise _invalid("invalid_schema", "A restoration render must contain repairs.")
    if type(payload["protected"]) is not list:
        raise _invalid("invalid_schema", "Render protected decisions must be an array.")
    pcm = _exact(
        payload["pcm_proof"],
        {
            "source_music_range_sha256",
            "restored_music_range_sha256",
            "outside_approved_windows_and_channels_identical",
            "approved_patches_match_receipt_hashes",
        },
        "Render PCM proof",
    )
    _digest(pcm["source_music_range_sha256"], "Render source PCM SHA-256")
    _digest(pcm["restored_music_range_sha256"], "Render restored PCM SHA-256")
    _true(pcm["outside_approved_windows_and_channels_identical"], "Render outside-window proof")
    _true(pcm["approved_patches_match_receipt_hashes"], "Render patch proof")
    proof = _exact(
        payload["proof"],
        {
            "source_unchanged",
            "immutable_source_snapshot",
            "project_unchanged",
            "scan_unchanged",
            "recipe_unchanged",
            "lossless_flac_round_trip",
            "frame_count_equal_to_project_music_range",
            "format_equal_to_source",
        },
        "Render proof",
    )
    for key, value in proof.items():
        _true(value, f"Render proof {key}")
    files = _validated_files(bundle, payload["files"], {"restored": "restored.flac"}, render=True)
    restored = _exact(
        cast(dict[str, Any], payload["files"])["restored"],
        {"path", "sha256", "sample_count", "sample_rate", "channels", "bits_per_raw_sample"},
        "Restored output",
    )
    if _integer(restored["sample_count"], "Restored sample count", minimum=1) != count:
        raise _invalid("invalid_schema", "Restored output sample count is inconsistent.")
    _integer(restored["sample_rate"], "Restored sample rate", minimum=1)
    _integer(restored["channels"], "Restored channels", minimum=1, maximum=64)
    if _integer(restored["bits_per_raw_sample"], "Restored bit depth", minimum=1) not in {16, 24}:
        raise _invalid("invalid_schema", "Restored output bit depth is unsupported.")
    _verify_bundle_contents(bundle, {"render.json", "restored.flac"})
    return files


def _dependency_specs(
    kind: ArtifactKind, payload: Mapping[str, Any]
) -> tuple[tuple[ArtifactKind, str, str], ...]:
    if kind == "scan":
        return ()
    scan = cast(dict[str, Any], payload["scan"])
    specs: list[tuple[ArtifactKind, str, str]] = [
        ("scan", cast(str, scan["path"]), cast(str, scan["sha256"]))
    ]
    if kind == "render":
        recipe = cast(dict[str, Any], payload["recipe"])
        specs.append(("recipe", cast(str, recipe["path"]), cast(str, recipe["sha256"])))
    return tuple(specs)


def _load_provisional(kind: ArtifactKind, manifest: Path, bundle: Path | None) -> _Provisional:
    payload, manifest_sha = _load_manifest(manifest)
    if payload.get("schema") != _SCHEMAS[kind]:
        raise _invalid("wrong_schema", f"The {kind} manifest uses the wrong schema.")
    if kind == "scan":
        # Full semantic validation follows once current project context is known.
        created_at, created_at_utc = _provenance(payload)
        files: tuple[RestorationFile, ...] = ()
    elif kind == "recipe":
        _validate_recipe_shape(payload)
        created_at, created_at_utc = _provenance(payload)
        files = ()
    elif kind == "preview":
        if bundle is None:
            raise AssertionError("Preview bundle path is required.")
        files = _validate_preview_shape(payload, bundle)
        created_at, created_at_utc = _provenance(payload)
    else:
        if bundle is None:
            raise AssertionError("Render bundle path is required.")
        files = _validate_render_shape(payload, bundle)
        created_at, created_at_utc = _provenance(payload)
    return _Provisional(
        kind=kind,
        manifest_path=manifest,
        manifest_sha256=manifest_sha,
        artifact_id=f"{kind}-{manifest_sha[:32]}",
        created_at=created_at,
        created_at_utc=created_at_utc,
        payload=payload,
        files=files,
        dependency_specs=_dependency_specs(kind, payload),
    )


def _looks_like_artifact(name: str) -> bool:
    return name.startswith(("scan-", "recipe-", "preview-", "render-"))


def _kind_hint(name: str) -> ArtifactKind | None:
    for kind in cast(tuple[ArtifactKind, ...], tuple(_KIND_ORDER)):
        if name.startswith(f"{kind}-"):
            return kind
    return None


def _discover_entries(
    workspace: Path,
) -> tuple[
    list[tuple[ArtifactKind, Path, Path | None]],
    list[RestorationCatalogIssue],
]:
    try:
        entries: list[os.DirEntry[str]] = []
        with os.scandir(workspace) as iterator:
            for entry in iterator:
                entries.append(entry)
                if len(entries) > MAX_WORKSPACE_ENTRIES:
                    raise _invalid(
                        "workspace_entry_limit",
                        "The restoration workspace contains too many immediate entries.",
                    )
    except _InvalidArtifact:
        raise
    except OSError as exc:
        raise _invalid(
            "unreadable_workspace", "The restoration workspace could not be listed."
        ) from exc
    result: list[tuple[ArtifactKind, Path, Path | None]] = []
    issues: list[RestorationCatalogIssue] = []
    artifact_count = 0
    for entry in sorted(entries, key=lambda item: item.name):
        name = entry.name
        if not _looks_like_artifact(name):
            continue
        artifact_count += 1
        if artifact_count > MAX_ARTIFACTS:
            raise _invalid(
                "artifact_limit",
                "The restoration workspace contains too many artifacts.",
            )
        file_match = _FILE_NAME.fullmatch(name)
        bundle_match = _BUNDLE_NAME.fullmatch(name)
        path = workspace / name
        try:
            if file_match is not None:
                kind = cast(ArtifactKind, file_match.group(1))
                _safe_lstat(path, directory=False)
                result.append((kind, path, None))
            elif bundle_match is not None:
                kind = cast(ArtifactKind, bundle_match.group(1))
                _safe_lstat(path, directory=True)
                manifest = path / ("preview.json" if kind == "preview" else "render.json")
                result.append((kind, manifest, path))
            else:
                raise _invalid(
                    "unsafe_artifact_name",
                    f"Unsafe restoration artifact name: {name}",
                )
        except _InvalidArtifact as exc:
            issues.append(
                RestorationCatalogIssue(
                    path,
                    _kind_hint(name),
                    exc.code,
                    str(exc),
                )
            )
    return result, issues


def _current_reasons(
    raw: _Provisional,
    *,
    project_path: Path,
    project_sha256: str,
    source_path: Path,
    source_sha256: str,
) -> list[str]:
    reasons: list[str] = []
    payload = raw.payload
    if raw.kind in {"scan", "recipe", "render"}:
        binding = cast(dict[str, Any], payload["project"])
        if binding["path"] != project_path.name or binding["sha256"] != project_sha256:
            reasons.append("project_identity_changed")
    source = cast(dict[str, Any], payload["source"])
    if source["path"] != source_path.name or source["sha256"] != source_sha256:
        reasons.append("source_identity_changed")
    return reasons


def _resolve_dependencies(
    raw: _Provisional,
    by_name: Mapping[str, _Provisional],
    usable_ids: set[str],
) -> tuple[RestorationDependency, ...]:
    dependencies: list[RestorationDependency] = []
    for expected_kind, name, expected_sha in raw.dependency_specs:
        _basename(name, f"{raw.kind} {expected_kind} dependency")
        dependency = by_name.get(name)
        if dependency is None or dependency.kind != expected_kind:
            raise _invalid(
                "missing_dependency",
                f"The {raw.kind} artifact's {expected_kind} dependency is missing.",
            )
        if dependency.artifact_id not in usable_ids:
            raise _invalid(
                "invalid_dependency",
                f"The {raw.kind} artifact depends on a rejected {expected_kind} artifact.",
            )
        if dependency.manifest_sha256 != expected_sha:
            raise _invalid(
                "dependency_hash_mismatch",
                f"The {raw.kind} artifact's {expected_kind} dependency hash is stale or edited.",
            )
        dependencies.append(
            RestorationDependency(
                expected_kind,
                name,
                expected_sha,
                dependency.artifact_id,
            )
        )
    return tuple(dependencies)


def _validate_resolved_chain(raw: _Provisional, by_name: Mapping[str, _Provisional]) -> None:
    if raw.kind == "scan":
        return
    scan_dependency = by_name[raw.dependencies[0].name]
    scan = scan_dependency.payload
    candidates_raw = cast(list[Any], scan["candidates"])
    candidates = {
        cast(str, cast(dict[str, Any], item)["id"]): cast(dict[str, Any], item)
        for item in candidates_raw
    }
    scan_source = cast(dict[str, Any], scan["source"])
    scan_project = cast(dict[str, Any], scan["project"])
    source_binding = cast(dict[str, Any], raw.payload["source"])
    if (
        source_binding["path"] != scan_source["path"]
        or source_binding["sha256"] != scan_source["sha256"]
    ):
        raise _invalid("dependency_binding_mismatch", "Artifact source and scan source disagree.")
    if raw.kind == "recipe":
        project_binding = cast(dict[str, Any], raw.payload["project"])
        if project_binding != scan_project:
            raise _invalid(
                "dependency_binding_mismatch", "Recipe project and scan project disagree."
            )
        try:
            _validate_recipe_payload(
                raw.payload,
                project_path=Path(cast(str, project_binding["path"])),
                project_sha256=cast(str, project_binding["sha256"]),
                source_path=Path(cast(str, source_binding["path"])),
                source_sha256=cast(str, source_binding["sha256"]),
                scan_path=scan_dependency.manifest_path,
                scan_sha256=scan_dependency.manifest_sha256,
                candidates=candidates,
                expected_coverage=cast(dict[str, Any], scan["coverage"]),
            )
        except GrooveSerpentError as exc:
            raise _invalid("invalid_schema", str(exc)) from exc
        return
    if raw.kind == "preview":
        preview_source = cast(dict[str, Any], raw.payload["source"])
        for key in ("sample_rate", "channels", "bits_per_raw_sample"):
            if preview_source[key] != scan_source[key]:
                raise _invalid(
                    "dependency_binding_mismatch", "Preview and scan audio geometry disagree."
                )
        selected = cast(list[Any], raw.payload["candidates"])
        selected_ids: list[str] = []
        for item in selected:
            if type(item) is not dict:
                raise _invalid(
                    "invalid_schema", "Preview candidates must be scan candidate objects."
                )
            candidate = cast(dict[str, Any], item)
            candidate_id = candidate.get("id")
            if not isinstance(candidate_id, str) or candidates.get(candidate_id) != candidate:
                raise _invalid(
                    "dependency_binding_mismatch", "A preview candidate differs from its scan."
                )
            selected_ids.append(candidate_id)
        if len(selected_ids) != len(set(selected_ids)):
            raise _invalid("invalid_schema", "Preview candidate IDs are duplicated.")
        expected_selected = sorted(
            (candidates[candidate_id] for candidate_id in selected_ids),
            key=lambda candidate: (
                cast(int, candidate["start_frame"]),
                cast(int, candidate["end_frame_exclusive"]),
                cast(list[int], candidate["channels"]),
            ),
        )
        if selected != expected_selected:
            raise _invalid("invalid_schema", "Preview candidates are not in canonical order.")
        context = cast(dict[str, Any], raw.payload["context"])
        windows = cast(list[Any], context["repair_windows"])
        context_start = cast(int, context["start_frame"])
        context_end = cast(int, context["end_frame_exclusive"])
        expected_windows: list[dict[str, Any]] = []
        for candidate_id in selected_ids:
            candidate = candidates[candidate_id]
            expected_windows.append(
                {
                    "candidate_id": candidate_id,
                    "start_in_preview": cast(int, candidate["start_frame"]) - context_start,
                    "end_in_preview_exclusive": cast(int, candidate["end_frame_exclusive"])
                    - context_start,
                    "channels": candidate["channels"],
                }
            )
        if windows != expected_windows:
            raise _invalid("invalid_schema", "Preview repair windows differ from scan candidates.")
        expected_local_start = (
            min(cast(int, candidate["start_frame"]) for candidate in expected_selected)
            - context_start
        )
        expected_local_end = (
            max(cast(int, candidate["end_frame_exclusive"]) for candidate in expected_selected)
            - context_start
        )
        if (
            context["repair_start_in_preview"] != expected_local_start
            or context["repair_end_in_preview_exclusive"] != expected_local_end
            or expected_local_start < 0
            or expected_local_end > context_end - context_start
        ):
            raise _invalid("invalid_schema", "Preview aggregate repair range is inconsistent.")
        metrics = cast(dict[str, Any], raw.payload["metrics"])
        for role in ("before", "proposed"):
            role_metrics = cast(dict[str, Any], metrics[role])
            boundaries = cast(list[Any], role_metrics["window_boundaries"])
            for boundary, window in zip(boundaries, expected_windows, strict=True):
                rendered_boundary = cast(dict[str, Any], boundary)
                if (
                    rendered_boundary["candidate_id"] != window["candidate_id"]
                    or rendered_boundary["channels"] != window["channels"]
                ):
                    raise _invalid(
                        "invalid_schema",
                        "Preview boundary metrics differ from repair windows.",
                    )
        return
    recipe_dependency = by_name[raw.dependencies[1].name]
    recipe = recipe_dependency.payload
    render_project = cast(dict[str, Any], raw.payload["project"])
    if render_project != scan_project or render_project != cast(dict[str, Any], recipe["project"]):
        raise _invalid("dependency_binding_mismatch", "Render project bindings disagree.")
    if source_binding != cast(dict[str, Any], recipe["source"]):
        raise _invalid("dependency_binding_mismatch", "Render source bindings disagree.")
    if cast(dict[str, Any], raw.payload["coverage"]) != cast(dict[str, Any], scan["coverage"]):
        raise _invalid("dependency_binding_mismatch", "Render and scan coverage disagree.")
    music = cast(dict[str, Any], raw.payload["music_range"])
    coverage = cast(dict[str, Any], raw.payload["coverage"])
    if (
        music["start_frame"] != coverage["music_start_frame"]
        or music["end_frame_exclusive"] != coverage["music_end_frame_exclusive"]
        or music["sample_count"] != coverage["music_frame_count"]
    ):
        raise _invalid("invalid_schema", "Render music range and coverage disagree.")
    decisions = cast(list[Any], recipe["decisions"])
    approved = {
        cast(str, cast(dict[str, Any], decision)["candidate_id"])
        for decision in decisions
        if cast(dict[str, Any], decision)["decision"] == "approved"
    }
    protected = {
        cast(str, cast(dict[str, Any], decision)["candidate_id"]): cast(
            str, cast(dict[str, Any], decision)["classification"]
        )
        for decision in decisions
        if cast(dict[str, Any], decision)["decision"] == "protected"
    }
    repairs = cast(list[Any], raw.payload["repairs"])
    observed_approved: set[str] = set()
    observed_approved_order: list[str] = []
    for item in repairs:
        repair = _exact(
            item,
            {
                "candidate_id",
                "start_frame",
                "end_frame_exclusive",
                "channels",
                "source_pcm_sha256",
                "restored_pcm_sha256",
                "changed_scalar_samples",
            },
            "Render repair",
        )
        candidate_id = repair["candidate_id"]
        if (
            not isinstance(candidate_id, str)
            or candidate_id not in approved
            or candidate_id in observed_approved
        ):
            raise _invalid(
                "invalid_schema", "Render repairs do not match approved recipe decisions."
            )
        candidate = candidates[candidate_id]
        if any(
            repair[key] != candidate[key]
            for key in ("start_frame", "end_frame_exclusive", "channels")
        ):
            raise _invalid("invalid_schema", "Render repair bounds differ from the scan candidate.")
        _digest(repair["source_pcm_sha256"], "Repair source PCM SHA-256")
        _digest(repair["restored_pcm_sha256"], "Repair restored PCM SHA-256")
        _integer(repair["changed_scalar_samples"], "Repair changed samples", minimum=1)
        observed_approved.add(candidate_id)
        observed_approved_order.append(candidate_id)
    if observed_approved != approved:
        raise _invalid("invalid_schema", "Render repairs omit approved recipe decisions.")
    expected_approved_order = sorted(
        approved,
        key=lambda candidate_id: (
            cast(int, candidates[candidate_id]["start_frame"]),
            cast(int, candidates[candidate_id]["end_frame_exclusive"]),
            cast(list[int], candidates[candidate_id]["channels"]),
        ),
    )
    if observed_approved_order != expected_approved_order:
        raise _invalid("invalid_schema", "Render repairs are not in canonical order.")
    protected_items = cast(list[Any], raw.payload["protected"])
    observed_protected: dict[str, str] = {}
    for item in protected_items:
        entry = _exact(item, {"candidate_id", "classification"}, "Render protected decision")
        candidate_id = entry["candidate_id"]
        classification = entry["classification"]
        if not isinstance(candidate_id, str) or not isinstance(classification, str):
            raise _invalid("invalid_schema", "Render protected decisions are invalid.")
        if candidate_id in observed_protected:
            raise _invalid("invalid_schema", "Render protected decisions are duplicated.")
        observed_protected[candidate_id] = classification
    if observed_protected != protected:
        raise _invalid("invalid_schema", "Render protected decisions differ from the recipe.")
    restored = cast(dict[str, Any], cast(dict[str, Any], raw.payload["files"])["restored"])
    for key in ("sample_rate", "channels", "bits_per_raw_sample"):
        if restored[key] != scan_source[key]:
            raise _invalid(
                "dependency_binding_mismatch",
                "Restored audio geometry differs from the scan source.",
            )


def _as_artifact(raw: _Provisional) -> RestorationArtifact:
    return RestorationArtifact(
        artifact_id=raw.artifact_id,
        kind=raw.kind,
        manifest_path=raw.manifest_path,
        manifest_sha256=raw.manifest_sha256,
        created_at=raw.created_at,
        created_at_utc=raw.created_at_utc,
        payload=raw.payload,
        dependencies=raw.dependencies,
        files=raw.files,
        stale_reasons=raw.stale_reasons,
    )


def discover_restoration_catalog(
    workspace: Path | str,
    project_path: Path | str,
    *,
    verified_source_sha256: str | None = None,
) -> RestorationCatalog:
    """Discover one project's restoration workspace without modifying it.

    By default the current source is hashed directly.  A caller that already
    owns a verified immutable source snapshot may provide its lowercase digest
    to avoid a second album-sized read; the digest must match the project.
    """

    project_path = Path(project_path).expanduser().resolve()
    project, project_sha256 = load_project_with_sha256(project_path)
    source_path = resolve_source_path(project, project_path).resolve()
    expected_source_sha = project.source.sha256
    _digest(expected_source_sha, "Project source SHA-256")
    if verified_source_sha256 is None:
        try:
            source_sha256, source_size = _hash_regular_file(
                source_path,
                maximum_bytes=MAX_REFERENCED_FILE_BYTES,
            )
        except _InvalidArtifact as exc:
            raise ProjectValidationError(
                f"Current project source could not be verified: {exc}"
            ) from exc
        if source_size != project.source.size_bytes:
            raise ProjectValidationError("The current source size differs from the project.")
    else:
        source_sha256 = _digest(verified_source_sha256, "Verified source SHA-256")
    if source_sha256 != expected_source_sha:
        raise ProjectValidationError("The current source SHA-256 differs from the project.")

    workspace_path = Path(workspace).expanduser().absolute()
    empty = RestorationCatalog(
        workspace=workspace_path,
        project_path=project_path,
        project_sha256=project_sha256,
        source_path=source_path,
        source_sha256=source_sha256,
        artifacts=(),
        stale=(),
        invalid=(),
    )
    if not workspace_path.exists():
        return empty
    try:
        _safe_lstat(workspace_path, directory=True)
        discovered, discovery_issues = _discover_entries(workspace_path)
    except _InvalidArtifact as exc:
        return RestorationCatalog(
            workspace=workspace_path,
            project_path=project_path,
            project_sha256=project_sha256,
            source_path=source_path,
            source_sha256=source_sha256,
            artifacts=(),
            stale=(),
            invalid=(
                RestorationCatalogIssue(
                    workspace_path,
                    None,
                    exc.code,
                    str(exc),
                ),
            ),
        )

    provisional: list[_Provisional] = []
    issues = discovery_issues
    for kind, manifest, bundle in discovered:
        try:
            provisional.append(_load_provisional(kind, manifest, bundle))
        except _InvalidArtifact as exc:
            issues.append(RestorationCatalogIssue(manifest, kind, exc.code, str(exc)))

    # Full scan validation needs current project context but intentionally uses
    # the embedded source identity, allowing old valid scans to be classified
    # as stale instead of being mislabeled corrupt.
    structurally_valid: list[_Provisional] = []
    for raw in provisional:
        if raw.kind != "scan":
            structurally_valid.append(raw)
            continue
        try:
            _validate_scan(raw.payload, project, project_sha256)
            structurally_valid.append(raw)
        except _InvalidArtifact as exc:
            issues.append(RestorationCatalogIssue(raw.manifest_path, raw.kind, exc.code, str(exc)))

    identity_groups: dict[str, list[_Provisional]] = {}
    for raw in structurally_valid:
        identity_groups.setdefault(raw.artifact_id, []).append(raw)
    duplicate_ids = {identity for identity, items in identity_groups.items() if len(items) != 1}
    for identity in sorted(duplicate_ids):
        for raw in identity_groups[identity]:
            issues.append(
                RestorationCatalogIssue(
                    raw.manifest_path,
                    raw.kind,
                    "duplicate_artifact_identity",
                    "More than one artifact has the same content-derived identity.",
                )
            )
    usable = [raw for raw in structurally_valid if raw.artifact_id not in duplicate_ids]
    by_name = {raw.manifest_path.name: raw for raw in usable if raw.kind in {"scan", "recipe"}}
    usable_ids = {raw.artifact_id for raw in usable}
    rejected_ids: set[str] = set()
    for kind in ("recipe", "preview", "render"):
        for raw in [item for item in usable if item.kind == kind]:
            try:
                raw.dependencies = _resolve_dependencies(
                    raw,
                    by_name,
                    usable_ids - rejected_ids,
                )
                _validate_resolved_chain(raw, by_name)
            except _InvalidArtifact as exc:
                rejected_ids.add(raw.artifact_id)
                issues.append(
                    RestorationCatalogIssue(raw.manifest_path, raw.kind, exc.code, str(exc))
                )

    accepted_raw = [raw for raw in usable if raw.artifact_id not in rejected_ids]
    raw_by_id = {raw.artifact_id: raw for raw in accepted_raw}
    for raw in sorted(accepted_raw, key=lambda item: _KIND_ORDER[item.kind]):
        reasons = _current_reasons(
            raw,
            project_path=project_path,
            project_sha256=project_sha256,
            source_path=source_path,
            source_sha256=source_sha256,
        )
        for dependency in raw.dependencies:
            dependency_raw = raw_by_id[dependency.artifact_id]
            if dependency_raw.stale_reasons:
                reasons.append(f"stale_{dependency.kind}_dependency")
        raw.stale_reasons = tuple(sorted(set(reasons)))

    current = tuple(
        sorted(
            (_as_artifact(raw) for raw in accepted_raw if not raw.stale_reasons),
            key=_artifact_sort_key,
        )
    )
    stale = tuple(
        sorted(
            (_as_artifact(raw) for raw in accepted_raw if raw.stale_reasons),
            key=_artifact_sort_key,
        )
    )
    invalid = tuple(
        sorted(
            issues,
            key=lambda issue: (
                issue.path.as_posix(),
                issue.kind or "",
                issue.code,
                issue.message,
            ),
        )
    )
    return RestorationCatalog(
        workspace_path,
        project_path,
        project_sha256,
        source_path,
        source_sha256,
        current,
        stale,
        invalid,
    )


__all__ = [
    "ArtifactKind",
    "MAX_ARTIFACTS",
    "MAX_BUNDLE_ENTRIES",
    "MAX_MANIFEST_BYTES",
    "MAX_WORKSPACE_ENTRIES",
    "RestorationArtifact",
    "RestorationCatalog",
    "RestorationCatalogIssue",
    "RestorationDependency",
    "RestorationFile",
    "RestorationSelection",
    "discover_restoration_catalog",
]
