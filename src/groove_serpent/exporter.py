from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import __version__
from .atomic_create import rename_no_replace
from .cache_storage import ensure_free_space
from .errors import ExportError, GrooveSerpentError, ProjectValidationError
from .media import find_tool, probe_audio, run_ffmpeg, tool_version
from .models import Project, Track, resolve_source_path, utc_now_iso
from .portable_names import (
    PortablePathError,
    normalize_portable_name,
    portable_name_key,
    portable_path_entry_exists,
    resolve_portable_path,
)
from .project_io import load_project_with_sha256
from .publication import (
    PUBLICATION_MANIFEST_SCHEMA,
    FileReceipt,
    assert_file_receipt,
    canonical_json_sha256,
    capture_file_receipt,
    stage_verified_copy,
)
from .subprocess_policy import (
    BoundedDiagnostic,
    join_diagnostic_reader,
    start_diagnostic_reader,
    terminate_and_reap,
)
from .validation import strict_finite_number

_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_MAX_ARTWORK_BYTES = 25 * 1024 * 1024
_ARTWORK_TYPES = {
    ".jpg": ("JPEG", b"\xff\xd8\xff"),
    ".jpeg": ("JPEG", b"\xff\xd8\xff"),
    ".png": ("PNG", b"\x89PNG\r\n\x1a\n"),
}
_MANIFEST_NAME = "groove-serpent-manifest.json"
_STAGING_PREFIX = ".groove-serpent-export-"
_STAGING_SUFFIX = ".partial"
_STORAGE_FILE_OVERHEAD_BYTES = 1024 * 1024
_PORTABLE_COMPONENT_UTF8_BYTES = 240
_PORTABLE_COMPONENT_UTF16_UNITS = 240
_TRUNCATED_NAME_HASH_HEX = 10


@dataclass(slots=True)
class ExportedFile:
    track_number: int
    format: str
    path: str
    size_bytes: int
    sha256: str
    expected_sample_count: int
    presentation_sample_count: int | None = None
    codec_name: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    bits_per_raw_sample: int | None = None
    decoded_pcm_sha256: str | None = None
    source_range_pcm_sha256: str | None = None
    complete_decode_verified: bool = False


@dataclass(slots=True)
class ExportReport:
    output_directory: str
    files: list[ExportedFile]
    manifest_path: str


@dataclass(frozen=True, slots=True)
class _StagedAudioVerification:
    codec_name: str
    sample_rate: int
    channels: int
    bits_per_raw_sample: int | None
    exact_sample_count: int
    presentation_sample_count: int | None
    decoded_pcm_sha256: str | None
    source_range_pcm_sha256: str | None


def _clean_filename_text(value: str) -> str:
    normalized = normalize_portable_name(value)
    # Lone surrogate code points cannot be encoded as UTF-8 and are not valid
    # portable filesystem text. Treat them like the other forbidden characters.
    normalized = "".join(
        "_" if 0xD800 <= ord(character) <= 0xDFFF else character
        for character in normalized
    )
    cleaned = _INVALID_FILENAME.sub("_", normalized).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _portable_component_fits(value: str) -> bool:
    return (
        len(value.encode("utf-8")) <= _PORTABLE_COMPONENT_UTF8_BYTES
        and len(value.encode("utf-16-le")) // 2 <= _PORTABLE_COMPONENT_UTF16_UNITS
    )


def sanitize_filename(
    value: str,
    fallback: str,
    *,
    prefix: str = "",
    suffix: str = "",
) -> str:
    """Return safe filename text within conservative encoded component budgets.

    ``prefix`` and ``suffix`` are not returned, but their encoded sizes are
    reserved. Callers that add track numbers or extensions therefore validate
    the complete filesystem component rather than the title in isolation.
    """

    prefix = normalize_portable_name(prefix)
    suffix = normalize_portable_name(suffix)
    fallback = _clean_filename_text(fallback) or "Track"
    cleaned = _clean_filename_text(value)
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    if _portable_component_fits(f"{prefix}{cleaned}{suffix}"):
        return cleaned

    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    hash_tag = f" ~{digest[:_TRUNCATED_NAME_HASH_HEX]}"
    fixed_text = f"{prefix}{hash_tag}{suffix}"
    remaining_utf8 = _PORTABLE_COMPONENT_UTF8_BYTES - len(fixed_text.encode("utf-8"))
    remaining_utf16 = _PORTABLE_COMPONENT_UTF16_UNITS - (
        len(fixed_text.encode("utf-16-le")) // 2
    )
    if remaining_utf8 < 0 or remaining_utf16 < 0:
        raise ValueError("The filename prefix and suffix exceed portable limits.")
    retained_characters: list[str] = []
    for character in cleaned:
        utf8_size = len(character.encode("utf-8"))
        utf16_size = len(character.encode("utf-16-le")) // 2
        if utf8_size > remaining_utf8 or utf16_size > remaining_utf16:
            break
        retained_characters.append(character)
        remaining_utf8 -= utf8_size
        remaining_utf16 -= utf16_size
    retained = "".join(retained_characters).rstrip()
    shortened = f"{retained}{hash_tag}"
    if not _portable_component_fits(f"{prefix}{shortened}{suffix}"):
        raise ValueError("The filename prefix and suffix exceed portable limits.")
    return shortened


def suggest_output_directory(project: Project, project_path: Path) -> Path:
    """Return a readable project-local batch path that does not yet exist."""

    project_path, _ = _resolve_portable_export_path(
        project_path,
        context="project path used for the export suggestion",
    )
    artist = str(
        project.metadata.get("album_artist") or project.metadata.get("artist") or ""
    ).strip()
    album = str(project.metadata.get("album") or "").strip()
    side = str(project.metadata.get("side") or "").strip()
    parts = [
        value for value in (artist, album, f"Side {side}" if side else "") if value
    ]
    label = " - ".join(parts)
    base = sanitize_filename(label, project_path.stem)
    root, root_exists = _resolve_portable_export_path(
        project_path.parent / "exports",
        context="export suggestion parent",
    )
    if root_exists and not root.is_dir():
        raise ExportError("The export suggestion parent is not a directory.")
    candidate = root / base
    candidate, candidate_exists = _resolve_portable_export_path(
        candidate,
        context="export batch suggestion",
    )
    if not candidate_exists:
        return candidate
    for index in range(2, 10_000):
        suffix = f" - batch {index:02d}"
        candidate = root / (
            f"{sanitize_filename(label, project_path.stem, suffix=suffix)}{suffix}"
        )
        candidate, candidate_exists = _resolve_portable_export_path(
            candidate,
            context="export batch suggestion",
        )
        if not candidate_exists:
            return candidate
    raise ExportError("Could not find a unique export batch directory suggestion.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_entry_exists(path: Path) -> bool:
    """Return true for exact or Unicode-portability-equivalent entries."""

    return portable_path_entry_exists(path)


def _resolve_portable_export_path(
    path: Path,
    *,
    context: str,
    create_parents: bool = False,
) -> tuple[Path, bool]:
    """Resolve a path without creating portable-equivalent ancestor trees."""

    try:
        resolution = resolve_portable_path(path, create_parents=create_parents)
        resolved = resolution.path.resolve()
    except (OSError, PortablePathError, RuntimeError) as exc:
        raise ExportError(f"The {context} is not portable-safe: {exc}") from exc
    return resolved, resolution.entry_exists


def _cleanup_staging_directory(stage_dir: Path, expected_parent: Path) -> None:
    """Remove only a staging entry created by this exporter beside its target."""

    if stage_dir.parent != expected_parent or not (
        stage_dir.name.startswith(_STAGING_PREFIX)
        and stage_dir.name.endswith(_STAGING_SUFFIX)
    ):
        raise ExportError(
            f"Refusing to remove an unexpected export staging path: {stage_dir}"
        )
    if not _path_entry_exists(stage_dir):
        return
    if stage_dir.is_symlink() or not stage_dir.is_dir():
        stage_dir.unlink()
    else:
        shutil.rmtree(stage_dir)


def _cover_art_details(
    project: Project, project_path: Path
) -> tuple[Path | None, str | None, str | None]:
    if "cover_art_path" not in project.metadata:
        return None, None, None

    raw_path = project.metadata["cover_art_path"]
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ExportError(
            "Cover artwork must be a non-empty relative JPEG or PNG path."
        )

    supplied_path = Path(raw_path)
    if supplied_path.is_absolute():
        raise ExportError("Cover artwork path must be relative to the project folder.")

    project_root = project_path.expanduser().resolve().parent
    try:
        artwork_path = (project_root / supplied_path).resolve()
        relative_path = artwork_path.relative_to(project_root).as_posix()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExportError(
            "Cover artwork path must remain inside the project folder."
        ) from exc

    try:
        if not artwork_path.is_file():
            raise ExportError("Cover artwork does not exist or is not a regular file.")
        size_bytes = artwork_path.stat().st_size
    except OSError as exc:
        raise ExportError("Cover artwork could not be read as a regular file.") from exc
    if size_bytes > _MAX_ARTWORK_BYTES:
        raise ExportError("Cover artwork exceeds the 25 MB export limit.")

    expected = _ARTWORK_TYPES.get(artwork_path.suffix.casefold())
    if expected is None:
        raise ExportError("Cover artwork must use a .jpg, .jpeg, or .png extension.")
    image_type, signature = expected
    try:
        with artwork_path.open("rb") as handle:
            prefix = handle.read(8)
        artwork_sha256 = _sha256(artwork_path)
    except OSError as exc:
        raise ExportError("Cover artwork could not be read.") from exc
    if not prefix.startswith(signature):
        raise ExportError(
            f"Cover artwork content does not match its {image_type} extension."
        )
    expected_sha256 = project.metadata.get("cover_art_sha256", "").strip().lower()
    if expected_sha256 and expected_sha256 != artwork_sha256:
        raise ExportError(
            "Cover artwork no longer matches the image selected for this project. "
            "Fetch or select the artwork again before exporting."
        )
    return artwork_path, relative_path, artwork_sha256


def _metadata_arguments(
    track: Track,
    total_tracks: int,
    project_metadata: Mapping[str, str] | None = None,
    source_speed_factor: float | None = None,
    effective_source_speed_factor: float | None = None,
    speed_correction_rate: int | None = None,
) -> list[str]:
    metadata = project_metadata or {}
    speed_factor_text = (
        f"{source_speed_factor:.9f}" if source_speed_factor is not None else ""
    )
    values = {
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "album_artist": track.album_artist,
        "date": track.year,
        "genre": track.genre,
        "track": f"{track.number}/{total_tracks}",
        "tracktotal": str(total_tracks),
        "grouping": f"Side {track.side}" if track.side else "",
        "vinyl_side": track.side,
        "disc": metadata.get("musicbrainz_medium_position", ""),
        "musicbrainz_albumid": metadata.get("musicbrainz_release_id", ""),
        "musicbrainz_releasegroupid": metadata.get("musicbrainz_release_group_id", ""),
        "musicbrainz_recordingid": track.musicbrainz_recording_id,
        "musicbrainz_trackid": track.musicbrainz_track_id,
        "barcode": metadata.get("barcode", ""),
        "publisher": metadata.get("label", ""),
        "catalog_number": metadata.get("catalog_number", ""),
        "groove_serpent_source_speed_factor": speed_factor_text,
        "groove_serpent_effective_speed_factor": (
            f"{effective_source_speed_factor:.12f}"
            if effective_source_speed_factor is not None
            else ""
        ),
        "groove_serpent_asetrate_hz": (
            str(speed_correction_rate) if speed_correction_rate is not None else ""
        ),
        "groove_serpent_speed_correction": (
            "pitch-and-tempo together; integer asetrate + libsoxr"
            if source_speed_factor is not None
            else ""
        ),
        "comment": (
            f"Split and speed-corrected by Groove Serpent; source factor {speed_factor_text}"
            if source_speed_factor is not None
            else "Split by Groove Serpent"
        ),
    }
    arguments: list[str] = []
    for key, value in values.items():
        if value:
            arguments.extend(["-metadata", f"{key}={value}"])
    return arguments


def _speed_correction_details(
    source_sample_rate: int, source_speed_factor: float
) -> tuple[int, float]:
    """Return FFmpeg's integer asetrate and its exact effective factor.

    The ``asetrate`` filter accepts a whole-number sample rate. Recording both
    the requested factor and the effective rational factor prevents manifests
    from implying sub-hertz precision that FFmpeg cannot render.
    """

    corrected_rate = math.floor(source_sample_rate / source_speed_factor + 0.5)
    if corrected_rate < 1:
        raise ExportError("The source speed factor produced an invalid sample rate.")
    return corrected_rate, source_sample_rate / corrected_rate


def _speed_corrected_sample(
    source_sample: int, source_sample_rate: int, speed_correction_rate: int
) -> int:
    """Map a source boundary onto the shared corrected output sample grid."""

    # Exact round-half-up arithmetic avoids binary-float drift and gives both
    # tracks beside a marker the identical corrected boundary.
    return (2 * source_sample * source_sample_rate + speed_correction_rate) // (
        2 * speed_correction_rate
    )


def _expected_track_sample_count(
    track: Track,
    source_sample_rate: int,
    source_speed_factor: float | None,
) -> int:
    """Return the exact presentation length promised for one exported track."""

    if source_speed_factor is None:
        return track.end_sample - track.start_sample
    speed_correction_rate, _ = _speed_correction_details(
        source_sample_rate, source_speed_factor
    )
    corrected_start = _speed_corrected_sample(
        track.start_sample, source_sample_rate, speed_correction_rate
    )
    corrected_end = _speed_corrected_sample(
        track.end_sample, source_sample_rate, speed_correction_rate
    )
    return corrected_end - corrected_start


def _estimate_export_storage_bytes(
    project: Project,
    formats: Iterable[str],
    source_speed_factor: float | None,
    *,
    artwork_size_bytes: int = 0,
) -> int:
    """Return a conservative peak-space estimate for one atomic track batch.

    The estimate covers the immutable source snapshot, one raw-PCM-sized stream
    budget per requested format (never less than the compressed source size),
    embedded artwork per output, and one MiB of container/manifest slack per
    staged file. The normal storage reserve is added by ``ensure_free_space``.
    """

    selected_formats = tuple(formats)
    presentation_samples = sum(
        _expected_track_sample_count(
            track, project.source.sample_rate, source_speed_factor
        )
        for track in project.tracks
    )
    source_bits = project.source.bits_per_raw_sample
    bytes_per_sample = max(2, ((source_bits if source_bits is not None else 32) + 7) // 8)
    raw_stream_bytes = (
        presentation_samples * project.source.channels * bytes_per_sample
    )
    stream_budget = max(project.source.size_bytes, raw_stream_bytes)
    output_files = len(project.tracks) * len(selected_formats)
    return (
        project.source.size_bytes
        + stream_budget * len(selected_formats)
        + artwork_size_bytes * (output_files + 1)
        + _STORAGE_FILE_OVERHEAD_BYTES * (output_files + 2)
    )


def _probe_m4a_presentation_sample_count(path: Path, expected_sample_rate: int) -> int:
    """Read the post-edit-list audio duration from an M4A as an exact sample count.

    AAC packets include encoder priming and padding, so decoding packet totals is
    not the same as measuring the presentation timeline. FFprobe's stream
    ``duration_ts`` and ``time_base`` describe the timeline after the container's
    edit list has been applied. Requiring an integral result makes the export fail
    closed instead of rounding away a timestamp error.
    """

    ffprobe = find_tool("ffprobe")
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,duration_ts,time_base",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise ExportError(
            f"FFprobe could not verify the staged M4A '{path.name}': "
            f"{completed.stderr.strip() or 'no diagnostic was returned'}"
        )
    try:
        payload = json.loads(completed.stdout)
        stream = payload["streams"][0]
        sample_rate = int(stream["sample_rate"])
        duration_ts = int(stream["duration_ts"])
        numerator_text, denominator_text = str(stream["time_base"]).split("/", 1)
        numerator = int(numerator_text)
        denominator = int(denominator_text)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ExportError(
            f"FFprobe did not return an exact presentation timeline for '{path.name}'."
        ) from exc
    if sample_rate != expected_sample_rate:
        raise ExportError(
            f"Staged M4A '{path.name}' has sample rate {sample_rate}, expected "
            f"{expected_sample_rate}."
        )
    if duration_ts <= 0 or numerator <= 0 or denominator <= 0:
        raise ExportError(
            f"Staged M4A '{path.name}' has an invalid presentation timeline."
        )
    scaled_duration = duration_ts * numerator * sample_rate
    presentation_samples, remainder = divmod(scaled_duration, denominator)
    if remainder:
        raise ExportError(
            f"Staged M4A '{path.name}' presentation duration is not an exact "
            "whole number of output samples."
        )
    return presentation_samples


def _probe_exact_audio_stream(path: Path) -> dict[str, Any]:
    """Probe one staged audio stream without trusting container duration rounding."""

    completed = subprocess.run(
        [
            find_tool("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            (
                "stream=codec_name,sample_rate,channels,bits_per_raw_sample,"
                "bits_per_sample,sample_fmt,duration_ts,time_base"
            ),
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise ExportError(
            f"FFprobe could not verify staged output '{path.name}': "
            f"{completed.stderr.strip() or 'no diagnostic was returned'}"
        )
    try:
        stream = json.loads(completed.stdout)["streams"][0]
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
        duration_ts = int(stream["duration_ts"])
        numerator_text, denominator_text = str(stream["time_base"]).split("/", 1)
        numerator = int(numerator_text)
        denominator = int(denominator_text)
        codec_name = str(stream["codec_name"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ExportError(
            f"FFprobe did not return exact audio stream details for '{path.name}'."
        ) from exc
    if (
        not codec_name
        or sample_rate <= 0
        or channels <= 0
        or duration_ts <= 0
        or numerator <= 0
        or denominator <= 0
    ):
        raise ExportError(
            f"Staged output '{path.name}' has invalid audio stream details."
        )
    scaled_duration = duration_ts * numerator * sample_rate
    exact_sample_count, remainder = divmod(scaled_duration, denominator)
    if remainder:
        raise ExportError(
            f"Staged output '{path.name}' duration is not an exact whole number of samples."
        )
    bit_value = stream.get("bits_per_raw_sample") or stream.get("bits_per_sample")
    try:
        bits = int(bit_value) if bit_value not in (None, "", "0", 0) else None
    except (TypeError, ValueError):
        bits = None
    return {
        "codec_name": codec_name,
        "sample_rate": sample_rate,
        "channels": channels,
        "bits_per_raw_sample": bits,
        "sample_format": stream.get("sample_fmt"),
        "exact_sample_count": exact_sample_count,
    }


def _probe_track_numbering_tags(path: Path) -> dict[str, str]:
    """Read only the track-position tags needed for publication verification.

    FFmpeg exposes the standard MP4/M4A ``trkn`` atom as one ``track=N/T``
    value.  It does not necessarily expose a second ``tracktotal`` tag.  FLAC,
    by contrast, has a separate Vorbis-comment field for the total.  Keeping
    those representations distinct avoids opting M4A files into FFmpeg's
    generic ``mdta`` tag mode just to manufacture a redundant custom key.
    """

    completed = subprocess.run(
        [
            find_tool("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format_tags:stream_tags",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise ExportError(
            f"FFprobe could not verify track numbering for '{path.name}': "
            f"{completed.stderr.strip() or 'no diagnostic was returned'}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ExportError(
            f"FFprobe returned invalid track metadata for '{path.name}'."
        ) from exc
    if not isinstance(payload, dict):
        raise ExportError(
            f"FFprobe returned invalid track metadata for '{path.name}'."
        )

    scopes: list[object] = []
    format_value = payload.get("format")
    if format_value is not None:
        if not isinstance(format_value, dict):
            raise ExportError(
                f"FFprobe returned invalid format metadata for '{path.name}'."
            )
        scopes.append(format_value.get("tags"))
    streams = payload.get("streams", [])
    if not isinstance(streams, list):
        raise ExportError(
            f"FFprobe returned invalid stream metadata for '{path.name}'."
        )
    for stream in streams:
        if not isinstance(stream, dict):
            raise ExportError(
                f"FFprobe returned invalid stream metadata for '{path.name}'."
            )
        scopes.append(stream.get("tags"))

    observed: dict[str, str] = {}
    for raw_tags in scopes:
        if raw_tags is None:
            continue
        if not isinstance(raw_tags, dict):
            raise ExportError(f"Audio tags are invalid for '{path.name}'.")
        for raw_key, raw_value in raw_tags.items():
            if not isinstance(raw_key, str) or not isinstance(raw_value, str):
                raise ExportError(f"Audio tags are invalid for '{path.name}'.")
            key = raw_key.casefold()
            if key not in {"track", "tracktotal"}:
                continue
            previous = observed.get(key)
            if previous is not None and previous != raw_value:
                raise ExportError(
                    f"Audio tag {key!r} conflicts between container scopes."
                )
            observed[key] = raw_value
    return observed


def _verify_track_numbering_tags(
    tags: Mapping[str, str],
    *,
    expected_track_number: int,
    expected_total_tracks: int,
    output_format: str,
) -> None:
    """Verify a format's native representation of track number and total."""

    track_value = tags.get("track")
    if track_value is None:
        raise ExportError("Staged audio is missing its track-number tag.")
    match = re.fullmatch(r"([1-9][0-9]*)(?:/([1-9][0-9]*))?", track_value)
    if match is None:
        raise ExportError(f"Staged audio has invalid track numbering {track_value!r}.")
    observed_number = int(match.group(1))
    embedded_total = int(match.group(2)) if match.group(2) is not None else None

    separate_total_value = tags.get("tracktotal")
    separate_total: int | None = None
    if separate_total_value is not None:
        if re.fullmatch(r"[1-9][0-9]*", separate_total_value) is None:
            raise ExportError(
                f"Staged audio has invalid track total {separate_total_value!r}."
            )
        separate_total = int(separate_total_value)

    if output_format == "flac" and separate_total is None:
        raise ExportError("Staged FLAC is missing its separate TRACKTOTAL tag.")
    if embedded_total is None and separate_total is None:
        raise ExportError("Staged audio track numbering does not include a total.")
    if (
        embedded_total is not None
        and separate_total is not None
        and embedded_total != separate_total
    ):
        raise ExportError("Staged audio has conflicting embedded and separate totals.")
    observed_total = embedded_total if embedded_total is not None else separate_total
    if (
        observed_number != expected_track_number
        or observed_total != expected_total_tracks
    ):
        raise ExportError(
            "Staged audio track numbering differs from the export plan: "
            f"observed {observed_number}/{observed_total}, expected "
            f"{expected_track_number}/{expected_total_tracks}."
        )


def _complete_decode(path: Path) -> None:
    try:
        run_ffmpeg(
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
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-f",
                "null",
                "-",
            ]
        )
    except GrooveSerpentError as exc:
        raise ExportError(
            f"Staged output '{path.name}' failed complete decode verification: "
            f"{str(exc).strip() or 'no diagnostic was returned'}"
        ) from exc


def _decoded_pcm_sha256(
    path: Path,
    *,
    sample_format: str,
    start_sample: int | None = None,
    end_sample: int | None = None,
) -> str:
    """Hash a complete streamed PCM decode, optionally over one exact source range."""

    codec = {"s16le": "pcm_s16le", "s32le": "pcm_s32le"}[sample_format]
    command = [
        find_tool("ffmpeg"),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
    ]
    if start_sample is not None or end_sample is not None:
        if start_sample is None or end_sample is None or end_sample <= start_sample:
            raise ExportError("A decoded PCM verification range is invalid.")
        command.extend(
            [
                "-af",
                f"atrim=start_sample={start_sample}:end_sample={end_sample},asetpts=N",
            ]
        )
    command.extend(["-c:a", codec, "-f", sample_format, "pipe:1"])

    digest = hashlib.sha256()
    process: subprocess.Popen[bytes] | None = None
    diagnostic_capture: BoundedDiagnostic | None = None
    diagnostic_thread = None
    completed = False
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout = process.stdout
        if stdout is None or process.stderr is None:
            raise ExportError("FFmpeg did not expose its decoded PCM pipes.")
        diagnostic_capture, diagnostic_thread = start_diagnostic_reader(
            process,
            name="groove-serpent-pcm-stderr",
        )
        for chunk in iter(lambda: stdout.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
        stdout.close()
        return_code = process.wait()
        join_diagnostic_reader(process, diagnostic_thread)
        diagnostic = diagnostic_capture.text() if diagnostic_capture else ""
        completed = True
    finally:
        if process is not None and process.stdout is not None:
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
        if not completed:
            terminate_and_reap(process)
        join_diagnostic_reader(process, diagnostic_thread)
    if return_code != 0:
        raise ExportError(
            f"Staged audio '{path.name}' failed PCM integrity decoding: "
            f"{diagnostic or 'no diagnostic was returned'}"
        )
    return digest.hexdigest()


def _verify_staged_output(
    *,
    staged_path: Path,
    source_snapshot: Path,
    track: Track,
    output_format: str,
    expected_sample_count: int,
    source_sample_rate: int,
    source_channels: int,
    source_bits: int | None,
    source_speed_factor: float | None,
    total_tracks: int,
) -> _StagedAudioVerification:
    details = _probe_exact_audio_stream(staged_path)
    expected_codec = "flac" if output_format == "flac" else "aac"
    if details["codec_name"] != expected_codec:
        raise ExportError(
            f"Staged {output_format.upper()} '{staged_path.name}' uses codec "
            f"{details['codec_name']!r}; expected {expected_codec!r}."
        )
    if details["sample_rate"] != source_sample_rate:
        raise ExportError(
            f"Staged output '{staged_path.name}' has sample rate "
            f"{details['sample_rate']}; expected {source_sample_rate}."
        )
    if details["channels"] != source_channels:
        raise ExportError(
            f"Staged output '{staged_path.name}' has {details['channels']} channels; "
            f"expected {source_channels}."
        )
    if details["exact_sample_count"] != expected_sample_count:
        raise ExportError(
            f"Staged output '{staged_path.name}' has {details['exact_sample_count']} "
            f"samples; expected exactly {expected_sample_count}."
        )

    track_tags = _probe_track_numbering_tags(staged_path)
    _verify_track_numbering_tags(
        track_tags,
        expected_track_number=track.number,
        expected_total_tracks=total_tracks,
        output_format=output_format,
    )

    presentation_sample_count: int | None = None
    decoded_pcm_sha256: str | None = None
    source_range_pcm_sha256: str | None = None
    if output_format == "m4a":
        presentation_sample_count = _probe_m4a_presentation_sample_count(
            staged_path, source_sample_rate
        )
        if presentation_sample_count != expected_sample_count:
            raise ExportError(
                f"Staged M4A '{staged_path.name}' has {presentation_sample_count} "
                f"presentation samples; expected exactly {expected_sample_count}. "
                "The incomplete batch was not published."
            )
        _complete_decode(staged_path)
    else:
        declared_bits = details["bits_per_raw_sample"]
        if declared_bits is None:
            raise ExportError(
                f"Staged FLAC '{staged_path.name}' does not declare its PCM precision."
            )
        expected_bits = 24 if source_bits is not None and source_bits > 16 else 16
        if source_bits is not None and declared_bits != expected_bits:
            raise ExportError(
                f"Staged FLAC '{staged_path.name}' declares {declared_bits}-bit PCM; "
                f"expected {expected_bits}-bit PCM."
            )
        pcm_format = "s32le" if declared_bits > 16 else "s16le"
        decoded_pcm_sha256 = _decoded_pcm_sha256(staged_path, sample_format=pcm_format)
        if source_speed_factor is None and source_bits is not None:
            source_range_pcm_sha256 = _decoded_pcm_sha256(
                source_snapshot,
                sample_format=pcm_format,
                start_sample=track.start_sample,
                end_sample=track.end_sample,
            )
            if decoded_pcm_sha256 != source_range_pcm_sha256:
                raise ExportError(
                    f"Archival FLAC '{staged_path.name}' does not decode to the exact "
                    "selected source PCM range."
                )

    return _StagedAudioVerification(
        codec_name=details["codec_name"],
        sample_rate=details["sample_rate"],
        channels=details["channels"],
        bits_per_raw_sample=details["bits_per_raw_sample"],
        exact_sample_count=details["exact_sample_count"],
        presentation_sample_count=presentation_sample_count,
        decoded_pcm_sha256=decoded_pcm_sha256,
        source_range_pcm_sha256=source_range_pcm_sha256,
    )


def _output_path(output_dir: Path, track: Track, extension: str) -> Path:
    prefix = f"{track.number:02d} - "
    suffix = f".{extension}"
    title = sanitize_filename(
        track.title,
        f"Track {track.number:02d}",
        prefix=prefix,
        suffix=suffix,
    )
    return output_dir / f"{prefix}{title}{suffix}"


def _album_track_numbering(project: Project) -> tuple[int, int]:
    """Return a side-project number offset and the full album track total.

    A continuous album capture may contain a non-exportable side-change gap.
    Groove Serpent keeps each side as a contiguous project, while these optional
    metadata values preserve album-wide filenames and tags for Side B onward.
    """

    def metadata_integer(key: str, default: int, *, allow_zero: bool) -> int:
        raw = project.metadata.get(key, "")
        if raw == "":
            return default
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw):
            raise ExportError(f"Project metadata {key!r} must be a whole number.")
        value = int(raw)
        minimum = 0 if allow_zero else 1
        if not minimum <= value <= 9_999:
            qualifier = "between 0 and 9999" if allow_zero else "between 1 and 9999"
            raise ExportError(f"Project metadata {key!r} must be {qualifier}.")
        return value

    offset = metadata_integer("track_number_offset", 0, allow_zero=True)
    default_total = offset + len(project.tracks)
    total = metadata_integer("album_track_total", default_total, allow_zero=False)
    if offset + len(project.tracks) > total:
        raise ExportError(
            "Album track numbering extends past the declared album_track_total."
        )
    return offset, total


def _build_command(
    *,
    source_path: Path,
    output_path: Path,
    track: Track,
    total_tracks: int,
    output_format: str,
    source_sample_rate: int,
    source_bits: int | None,
    overwrite: bool,
    flac_compression: int,
    aac_bitrate: str,
    artwork_path: Path | None = None,
    project_metadata: Mapping[str, str] | None = None,
    source_speed_factor: float | None = None,
) -> list[str]:
    ffmpeg = find_tool("ffmpeg")
    filter_parts: list[str] = []
    effective_source_speed_factor: float | None = None
    speed_correction_rate: int | None = None
    if source_speed_factor is not None:
        speed_correction_rate, effective_source_speed_factor = (
            _speed_correction_details(source_sample_rate, source_speed_factor)
        )
        filter_parts.extend(
            [
                f"asetrate={speed_correction_rate}",
                (
                    f"aresample={source_sample_rate}:resampler=soxr:"
                    "precision=33:cutoff=0.99"
                ),
            ]
        )
        corrected_start = _speed_corrected_sample(
            track.start_sample, source_sample_rate, speed_correction_rate
        )
        corrected_end = _speed_corrected_sample(
            track.end_sample, source_sample_rate, speed_correction_rate
        )
        filter_parts.append(
            f"atrim=start_sample={corrected_start}:end_sample={corrected_end}"
        )
    else:
        filter_parts.append(
            f"atrim=start_sample={track.start_sample}:end_sample={track.end_sample}"
        )
    filter_parts.extend([f"asettb=expr=1/{source_sample_rate}", "asetpts=N"])
    filter_expression = ",".join(filter_parts)
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
        "-i",
        str(source_path),
    ]
    if artwork_path is not None:
        command.extend(["-i", str(artwork_path)])
    command.extend(
        [
            "-map",
            "0:a:0",
        ]
    )
    if artwork_path is not None:
        command.extend(["-map", "1:v:0"])
    else:
        command.append("-vn")
    command.extend(
        [
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-af",
            filter_expression,
            "-ar",
            str(source_sample_rate),
            *_metadata_arguments(
                track,
                total_tracks,
                project_metadata,
                source_speed_factor,
                effective_source_speed_factor,
                speed_correction_rate,
            ),
        ]
    )
    if output_format == "flac":
        command.extend(["-c:a", "flac", "-compression_level", str(flac_compression)])
        if source_bits is not None:
            command.extend(["-sample_fmt", "s32" if source_bits > 16 else "s16"])
    elif output_format == "m4a":
        command.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                aac_bitrate,
                "-movflags",
                "+faststart",
                "-movie_timescale",
                str(source_sample_rate),
                "-f",
                "ipod",
            ]
        )
    else:
        raise ExportError(f"Unsupported export format: {output_format}")
    if artwork_path is not None:
        command.extend(
            [
                "-c:v",
                "copy",
                "-disposition:v:0",
                "attached_pic",
                "-metadata:s:v:0",
                "title=Album cover",
                "-metadata:s:v:0",
                "comment=Cover (front)",
            ]
        )
    command.append(str(output_path))
    return command


def render_verified_track(
    *,
    source_snapshot: Path,
    staged_path: Path,
    track: Track,
    total_tracks: int,
    output_format: str,
    expected_sample_count: int,
    source_sample_rate: int,
    source_channels: int,
    source_bits: int | None,
    flac_compression: int,
    aac_bitrate: str,
    artwork_path: Path | None = None,
    project_metadata: Mapping[str, str] | None = None,
    source_speed_factor: float | None = None,
) -> _StagedAudioVerification:
    """Render and fully verify one track inside a caller-owned private stage.

    The caller owns atomic publication and source-snapshot identity.  This
    helper deliberately accepts no command fragments: it always uses Groove
    Serpent's fixed FFmpeg construction and the same codec, geometry, exact
    length, complete-decode, and lossless PCM checks as ``export_project``.
    """

    command = _build_command(
        source_path=source_snapshot,
        output_path=staged_path,
        track=track,
        total_tracks=total_tracks,
        output_format=output_format,
        source_sample_rate=source_sample_rate,
        source_bits=source_bits,
        overwrite=False,
        flac_compression=flac_compression,
        aac_bitrate=aac_bitrate,
        artwork_path=artwork_path,
        project_metadata=project_metadata,
        source_speed_factor=source_speed_factor,
    )
    run_ffmpeg(command)
    if not staged_path.is_file():
        raise ExportError(
            f"FFmpeg did not create the expected staged file: {staged_path.name}"
        )
    return _verify_staged_output(
        staged_path=staged_path,
        source_snapshot=source_snapshot,
        track=track,
        output_format=output_format,
        expected_sample_count=expected_sample_count,
        source_sample_rate=source_sample_rate,
        source_channels=source_channels,
        source_bits=source_bits,
        source_speed_factor=source_speed_factor,
        total_tracks=total_tracks,
    )


def export_project(
    project: Project,
    project_path: Path,
    output_dir: Path,
    *,
    formats: Iterable[str] = ("flac", "m4a"),
    overwrite: bool = False,
    flac_compression: int = 8,
    aac_bitrate: str = "256k",
    source_speed_factor: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> ExportReport:
    project.validate()
    operation_started_at = utc_now_iso()
    operation_project = Project.from_dict(project.to_dict())
    project_path = project_path.expanduser().resolve()
    operation_project_sha256 = canonical_json_sha256(operation_project.to_dict())
    editable_state_sha256 = operation_project.state_sha256

    requested = []
    for value in formats:
        if not isinstance(value, str):
            raise ExportError("Formats must be FLAC and/or M4A.")
        normalized = value.strip().lower()
        if normalized == "aac":
            normalized = "m4a"
        if normalized not in {"flac", "m4a"}:
            raise ExportError("Formats must be FLAC and/or M4A.")
        if normalized not in requested:
            requested.append(normalized)
    if not requested:
        raise ExportError("At least one output format is required.")
    if type(flac_compression) is not int or not 0 <= flac_compression <= 12:
        raise ExportError("FLAC compression must be between 0 and 12.")
    if not isinstance(aac_bitrate, str):
        raise ExportError("AAC bitrate must be text such as '256k'.")
    bitrate_match = re.fullmatch(r"([1-9][0-9]{1,3})k", aac_bitrate)
    if bitrate_match is None or not 32 <= int(bitrate_match.group(1)) <= 512:
        raise ExportError("AAC bitrate must be a whole value from 32k through 512k.")
    if "flac" in requested and (
        operation_project.source.bits_per_raw_sample is not None
        and operation_project.source.bits_per_raw_sample > 24
    ):
        raise ExportError(
            "FLAC export cannot preserve source precision above 24 bits with FFmpeg. "
            "Export was stopped before writing any files."
        )
    if "m4a" in requested and operation_project.source.sample_rate > 96_000:
        raise ExportError(
            "AAC/M4A export supports source sample rates up to 96 kHz without resampling. "
            "Export FLAC only, or explicitly create a portable resampled derivative in "
            "another tool."
        )
    if source_speed_factor is not None:
        try:
            source_speed_factor = strict_finite_number(
                source_speed_factor, "The source speed factor"
            )
        except ProjectValidationError as exc:
            raise ExportError(
                "The source speed factor must be finite and between 0.25 and 2.0. "
                "A factor above 1 means the capture runs fast and the derivative is "
                "slowed."
            ) from exc
        if not 0.25 <= source_speed_factor <= 2.0:
            raise ExportError(
                "The source speed factor must be finite and between 0.25 and 2.0. "
                "A factor above 1 means the capture runs fast and the derivative is "
                "slowed."
            )

    output_dir, output_exists = _resolve_portable_export_path(
        output_dir,
        context="output directory",
    )
    manifest_path = output_dir / _MANIFEST_NAME
    if output_exists:
        overwrite_note = (
            " The --overwrite option cannot replace a published batch."
            if overwrite
            else ""
        )
        raise ExportError(
            "The output directory already exists. Choose a new batch directory."
            + overwrite_note
        )

    project_file_receipt: FileReceipt | None = None
    project_file_sha256: str | None = None
    if _path_entry_exists(project_path):
        if not project_path.is_file():
            raise ExportError("The project path is not a regular file.")
        project_file_receipt = capture_file_receipt(project_path, label="Project file")
        try:
            disk_project, project_file_sha256 = load_project_with_sha256(project_path)
        except Exception as exc:
            raise ExportError(f"The project file could not be verified: {exc}") from exc
        if project_file_sha256 != project_file_receipt.sha256:
            raise ExportError("The project file changed while export was starting.")
        if (
            disk_project.revision != operation_project.revision
            or disk_project.state_sha256 != editable_state_sha256
            or disk_project.source.sha256.lower()
            != operation_project.source.sha256.lower()
            or canonical_json_sha256(disk_project.to_dict()) != operation_project_sha256
        ):
            raise ExportError(
                "The supplied project state does not exactly match the persisted project file. "
                "Save or reload the project before exporting."
            )

    artwork_path, artwork_relative_path, artwork_sha256 = _cover_art_details(
        operation_project, project_path
    )
    artwork_receipt: FileReceipt | None = None
    if artwork_path is not None:
        artwork_receipt = capture_file_receipt(artwork_path, label="Cover artwork")
        if artwork_receipt.sha256 != artwork_sha256:
            raise ExportError("Cover artwork changed while export was starting.")

    source_path = resolve_source_path(operation_project, project_path)
    source_receipt = capture_file_receipt(source_path, label="Source audio")
    current_source = probe_audio(source_path)
    if (
        not operation_project.source.sha256
        or current_source.sha256.lower() != source_receipt.sha256
        or source_receipt.sha256 != operation_project.source.sha256.lower()
        or current_source.size_bytes != operation_project.source.size_bytes
        or current_source.sample_rate != operation_project.source.sample_rate
        or current_source.channels != operation_project.source.channels
        or abs(
            current_source.duration_seconds - operation_project.source.duration_seconds
        )
        > 0.05
    ):
        raise ExportError(
            "The source audio no longer matches the file that was analyzed. "
            "Create a new project or restore the original source before exporting."
        )

    toolchain = {
        "ffmpeg": tool_version("ffmpeg"),
        "ffprobe": tool_version("ffprobe"),
    }

    track_number_offset, total_tracks = _album_track_numbering(operation_project)
    export_plan: list[tuple[Track, str, str, int]] = []
    seen_names = {portable_name_key(_MANIFEST_NAME): _MANIFEST_NAME}
    for track in operation_project.tracks:
        export_track = replace(track, number=track.number + track_number_offset)
        for output_format in requested:
            planned_path = _output_path(output_dir, export_track, output_format)
            if planned_path.parent != output_dir:
                raise ExportError("An export filename escaped the output directory.")
            filename = planned_path.name
            folded = portable_name_key(filename)
            previous = seen_names.get(folded)
            if previous is not None:
                raise ExportError(
                    "Export filenames collide on case-insensitive filesystems: "
                    f"{previous!r} and {filename!r}."
                )
            seen_names[folded] = filename
            export_plan.append(
                (
                    export_track,
                    output_format,
                    filename,
                    _expected_track_sample_count(
                        export_track,
                        operation_project.source.sample_rate,
                        source_speed_factor,
                    ),
                )
            )

    speed_plan: dict[str, Any] | None = None
    if source_speed_factor is not None:
        speed_correction_rate, effective_source_speed_factor = (
            _speed_correction_details(
                operation_project.source.sample_rate, source_speed_factor
            )
        )
        speed_plan = {
            "source_speed_factor": source_speed_factor,
            "effective_source_speed_factor": effective_source_speed_factor,
            "asetrate_hz": speed_correction_rate,
            "meaning": (
                "source playback rate divided by reference rate; values above 1 "
                "are slowed and pitch-lowered together"
            ),
            "method": (
                "integer asetrate, libsoxr precision 33, global-grid atrim, "
                "output at source rate"
            ),
            "boundary_mapping": (
                "round-half-up(source_sample * output_sample_rate / asetrate_hz)"
            ),
            "output_sample_rate": operation_project.source.sample_rate,
        }

    output_profile = {
        "name": "archival" if source_speed_factor is None else "speed-corrected",
        "restoration": "none",
        "fixed_speed_correction": source_speed_factor is not None,
        "derivatives": {
            output_format: (
                "lossless FLAC"
                if output_format == "flac"
                else "lossy portable AAC in M4A"
            )
            for output_format in requested
        },
    }
    available_encoder_settings = {
        "flac": {
            "encoder": "FFmpeg flac",
            "compression_level": flac_compression,
            "sample_precision": (
                operation_project.source.bits_per_raw_sample
                if operation_project.source.bits_per_raw_sample is not None
                else "encoder-selected; verified after encoding"
            ),
        },
        "m4a": {
            "encoder": "FFmpeg native aac",
            "bitrate": aac_bitrate,
            "container": "ipod/M4A",
            "movie_timescale": operation_project.source.sample_rate,
        },
    }
    encoder_settings = {
        output_format: available_encoder_settings[output_format]
        for output_format in requested
    }
    processing_plan = {
        "schema": "groove-serpent.processing-plan/1",
        "groove_serpent_version": __version__,
        "project_revision": operation_project.revision,
        "project_file_sha256": project_file_sha256,
        "editable_state_sha256": editable_state_sha256,
        "operation_project_sha256": operation_project_sha256,
        "source_sha256": source_receipt.sha256,
        "artwork_sha256": artwork_receipt.sha256 if artwork_receipt else None,
        "output_profile": output_profile,
        "operation_order": [
            "verified immutable source snapshot",
            "optional fixed speed correction",
            "exact global-grid track trim",
            "encode and tag",
            "probe and complete-decode verification",
            "atomic batch publication",
        ],
        "formats": requested,
        "encoder_settings": encoder_settings,
        "speed_correction": speed_plan,
        "tracks": [
            {
                "track_number": track.number,
                "format": output_format,
                "path": filename,
                "source_start_sample": track.start_sample,
                "source_end_sample": track.end_sample,
                "expected_output_samples": expected_sample_count,
            }
            for track, output_format, filename, expected_sample_count in export_plan
        ],
        "toolchain": toolchain,
    }
    processing_plan_sha256 = canonical_json_sha256(processing_plan)

    output_dir, output_appeared = _resolve_portable_export_path(
        output_dir,
        context="output directory",
        create_parents=True,
    )
    manifest_path = output_dir / _MANIFEST_NAME
    if not output_dir.parent.is_dir():
        raise ExportError("The export parent path is not a directory.")
    if output_appeared:
        raise ExportError(
            "The output directory was created by another process. Choose a new batch directory."
        )

    storage_required = _estimate_export_storage_bytes(
        operation_project,
        requested,
        source_speed_factor,
        artwork_size_bytes=(
            artwork_receipt.size_bytes if artwork_receipt is not None else 0
        ),
    )
    try:
        ensure_free_space(
            output_dir.parent,
            storage_required,
            label="Track export",
        )
    except GrooveSerpentError as exc:
        raise ExportError(str(exc)) from exc

    stage_dir = output_dir.parent / (
        f"{_STAGING_PREFIX}{uuid.uuid4().hex}{_STAGING_SUFFIX}"
    )
    stage_created = False
    exported: list[ExportedFile] = []
    try:
        stage_dir.mkdir()
        stage_created = True
        operation_dir = stage_dir / ".operation-inputs"
        operation_dir.mkdir()
        source_snapshot = operation_dir / (
            "source" + (source_path.suffix.casefold() or ".audio")
        )
        source_snapshot_receipt = stage_verified_copy(
            source_path,
            source_snapshot,
            source_receipt,
            label="Source audio",
        )
        artwork_snapshot: Path | None = None
        artwork_snapshot_receipt: FileReceipt | None = None
        if artwork_path is not None and artwork_receipt is not None:
            artwork_snapshot = operation_dir / (
                "artwork" + artwork_path.suffix.casefold()
            )
            artwork_snapshot_receipt = stage_verified_copy(
                artwork_path,
                artwork_snapshot,
                artwork_receipt,
                label="Cover artwork",
            )

        for track, output_format, filename, expected_sample_count in export_plan:
            staged_path = stage_dir / filename
            if progress:
                progress(
                    f"Exporting track {track.number}/{total_tracks} as "
                    f"{output_format.upper()}: {track.title}"
                )
            verification = render_verified_track(
                source_snapshot=source_snapshot,
                staged_path=staged_path,
                track=track,
                total_tracks=total_tracks,
                output_format=output_format,
                expected_sample_count=expected_sample_count,
                source_sample_rate=operation_project.source.sample_rate,
                source_channels=operation_project.source.channels,
                source_bits=operation_project.source.bits_per_raw_sample,
                flac_compression=flac_compression,
                aac_bitrate=aac_bitrate,
                artwork_path=artwork_snapshot,
                project_metadata=operation_project.metadata,
                source_speed_factor=source_speed_factor,
            )
            exported.append(
                ExportedFile(
                    track_number=track.number,
                    format=output_format,
                    path=filename,
                    size_bytes=staged_path.stat().st_size,
                    sha256=_sha256(staged_path),
                    expected_sample_count=expected_sample_count,
                    presentation_sample_count=(verification.presentation_sample_count),
                    codec_name=verification.codec_name,
                    sample_rate=verification.sample_rate,
                    channels=verification.channels,
                    bits_per_raw_sample=verification.bits_per_raw_sample,
                    decoded_pcm_sha256=verification.decoded_pcm_sha256,
                    source_range_pcm_sha256=(verification.source_range_pcm_sha256),
                    complete_decode_verified=True,
                )
            )

        manifest = {
            "schema": PUBLICATION_MANIFEST_SCHEMA,
            "groove_serpent_version": __version__,
            "created_at": utc_now_iso(),
            "operation_started_at": operation_started_at,
            "project": project_path.name,
            "project_file_sha256": project_file_sha256,
            "project_revision": operation_project.revision,
            "editable_state_sha256": editable_state_sha256,
            "source": operation_project.source.filename,
            "source_sha256": source_receipt.sha256,
            "sample_rate": operation_project.source.sample_rate,
            "tracks": len(operation_project.tracks),
            "album_tracks": total_tracks,
            "track_number_offset": track_number_offset,
            "formats": requested,
            "output_profile": output_profile,
            "toolchain": toolchain,
            "encoder_settings": encoder_settings,
            "processing_plan": processing_plan,
            "processing_plan_sha256": processing_plan_sha256,
            "project_identity": {
                "path": project_path.name,
                "captured_at": operation_started_at,
                "file_present": project_file_receipt is not None,
                "project_file_sha256": project_file_sha256,
                "operation_snapshot_sha256": operation_project_sha256,
                "revision": operation_project.revision,
                "editable_state_sha256": editable_state_sha256,
                **(
                    {"size_bytes": project_file_receipt.size_bytes}
                    if project_file_receipt is not None
                    else {}
                ),
            },
            "source_identity": {
                "filename": operation_project.source.filename,
                "sha256": source_receipt.sha256,
                "size_bytes": source_receipt.size_bytes,
            },
            "verification": {
                "all_outputs_fully_probed": True,
                "all_outputs_completely_decoded": True,
                "m4a_exact_presentation_length": "required",
                "archival_flac_source_pcm_equality": (
                    "required"
                    if source_speed_factor is None
                    and operation_project.source.bits_per_raw_sample is not None
                    and "flac" in requested
                    else "not-applicable"
                ),
                "prepublication_input_revalidation": "matched",
                "publication": "atomic-directory-rename",
            },
            "files": [
                {
                    "track_number": item.track_number,
                    "format": item.format,
                    "path": item.path,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                    "expected_sample_count": item.expected_sample_count,
                    "verification": {
                        "codec_name": item.codec_name,
                        "sample_rate": item.sample_rate,
                        "channels": item.channels,
                        "bits_per_raw_sample": item.bits_per_raw_sample,
                        "exact_sample_count": item.expected_sample_count,
                        "complete_decode_verified": item.complete_decode_verified,
                        **(
                            {"decoded_pcm_sha256": item.decoded_pcm_sha256}
                            if item.decoded_pcm_sha256 is not None
                            else {}
                        ),
                        **(
                            {
                                "source_range_pcm_sha256": (
                                    item.source_range_pcm_sha256
                                ),
                                "archival_pcm_equal": True,
                            }
                            if item.source_range_pcm_sha256 is not None
                            else {}
                        ),
                    },
                    **(
                        {"presentation_sample_count": (item.presentation_sample_count)}
                        if item.presentation_sample_count is not None
                        else {}
                    ),
                }
                for item in exported
            ],
        }
        if speed_plan is not None:
            manifest["speed_correction"] = speed_plan
        if (
            artwork_relative_path is not None
            and artwork_sha256 is not None
            and artwork_receipt is not None
        ):
            manifest["artwork"] = {
                "path": artwork_relative_path,
                "sha256": artwork_receipt.sha256,
                "size_bytes": artwork_receipt.size_bytes,
            }
        staged_manifest = stage_dir / _MANIFEST_NAME
        with staged_manifest.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                manifest,
                handle,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        assert_file_receipt(
            source_snapshot,
            source_snapshot_receipt,
            label="Staged source snapshot",
        )
        if artwork_snapshot is not None and artwork_snapshot_receipt is not None:
            assert_file_receipt(
                artwork_snapshot,
                artwork_snapshot_receipt,
                label="Staged artwork snapshot",
            )
        shutil.rmtree(operation_dir)

        if canonical_json_sha256(project.to_dict()) != operation_project_sha256:
            raise ExportError(
                "The in-memory project changed during export; the staged batch was not published."
            )
        if project_file_receipt is not None:
            assert_file_receipt(
                project_path, project_file_receipt, label="Project file"
            )
        elif _path_entry_exists(project_path):
            raise ExportError(
                "A project file appeared during export; the staged batch was not published."
            )
        assert_file_receipt(source_path, source_receipt, label="Source audio")
        if artwork_path is not None and artwork_receipt is not None:
            assert_file_receipt(artwork_path, artwork_receipt, label="Cover artwork")

        if _path_entry_exists(output_dir):
            raise ExportError(
                "The output directory was created while this batch was staging; "
                "nothing was replaced."
            )
        rename_no_replace(stage_dir, output_dir)
        stage_created = False
    except BaseException as exc:
        cleanup_error: Exception | None = None
        if stage_created:
            try:
                _cleanup_staging_directory(stage_dir, output_dir.parent)
            except (
                Exception
            ) as cleanup_exc:  # pragma: no cover - rare filesystem failure
                cleanup_error = cleanup_exc
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, ExportError) and cleanup_error is None:
            raise
        if isinstance(exc, ExportError):
            message = str(exc)
        else:
            message = f"Export failed before a complete batch could be published: {exc}"
        if cleanup_error is not None:
            message += f" Staging cleanup also failed at {stage_dir}: {cleanup_error}"
        raise ExportError(message) from exc

    return ExportReport(
        output_directory=str(output_dir),
        files=exported,
        manifest_path=str(manifest_path),
    )
