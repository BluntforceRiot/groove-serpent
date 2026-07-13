"""Review-first file workflow for click scanning and lossless A/B previews."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator, Mapping, Sequence

import numpy as np

from . import __version__
from .audio_snapshot import VerifiedAudioSnapshot, verified_audio_snapshot
from .cache_storage import ensure_free_space, resolve_cache_root
from .errors import GrooveSerpentError, ProjectValidationError
from .media import find_tool, probe_audio, sha256_file, tool_version
from .models import Project, resolve_source_path, utc_now_iso
from .project_io import load_project
from .publication import (
    FileReceipt,
    assert_file_receipt,
    capture_file_receipt,
    stage_verified_copy,
)
from .restoration import (
    ClickInterval,
    MAX_REPAIR_SAMPLES,
    detect_clipped_runs,
    detect_impulsive_clicks,
    repair_click_intervals,
)
from .subprocess_policy import (
    BoundedDiagnostic,
    join_diagnostic_reader,
    start_diagnostic_reader,
    terminate_and_reap,
)
from .validation import strict_finite_number


SCAN_SCHEMA = "groove-serpent.click-scan/1"
PREVIEW_SCHEMA = "groove-serpent.click-preview/3"
RECIPE_SCHEMA = "groove-serpent.restoration-recipe/1"
RENDER_SCHEMA = "groove-serpent.restoration-render/1"
DETECTOR_NAME = "impulse-and-clipping-v1"
REPAIR_BACKEND = "bidirectional-lpc-hermite-v2"
MAX_PREVIEW_CANDIDATES = 8
CLIP_REPAIR_PADDING_SAMPLES = 16
REMOVED_SIGNAL_GAIN = 16.0
# Reserve a small fixed allowance beyond the conservative audio payload estimate
# for FLAC container framing plus render.json and its publication receipts.
RENDER_STORAGE_SLACK_BYTES = 1 << 20
_PROTECTED_CLASSIFICATIONS = {
    "needle-drop",
    "needle-pickup",
    "handling-event",
    "other-structural-event",
}


@dataclass(slots=True)
class _FileSnapshot:
    live_path: Path
    path: Path
    live_receipt: FileReceipt
    snapshot_receipt: FileReceipt
    temporary_directory: tempfile.TemporaryDirectory[str]
    label: str

    def assert_unchanged(self) -> None:
        assert_file_receipt(
            self.path,
            self.snapshot_receipt,
            label=f"Staged {self.label.lower()} snapshot",
        )
        assert_file_receipt(self.live_path, self.live_receipt, label=self.label)

    def close(self) -> None:
        self.temporary_directory.cleanup()


@dataclass(slots=True)
class _RestorationInputs:
    project_path: Path
    project_snapshot: _FileSnapshot
    project: Project
    source_path: Path
    source_snapshot: VerifiedAudioSnapshot
    source: Any
    spec: "_PcmSpec"
    owns_source_snapshot: bool
    scan_path: Path | None = None
    scan_snapshot: _FileSnapshot | None = None
    scan: dict[str, Any] | None = None
    recipe_path: Path | None = None
    recipe_snapshot: _FileSnapshot | None = None
    recipe: dict[str, Any] | None = None

    def assert_unchanged(self) -> None:
        self.project_snapshot.assert_unchanged()
        self.source_snapshot.assert_snapshot_unchanged()
        self.source_snapshot.assert_live_unchanged()
        if self.scan_snapshot is not None:
            self.scan_snapshot.assert_unchanged()
        if self.recipe_snapshot is not None:
            self.recipe_snapshot.assert_unchanged()

    def close(self) -> None:
        if self.recipe_snapshot is not None:
            self.recipe_snapshot.close()
        if self.scan_snapshot is not None:
            self.scan_snapshot.close()
        if self.owns_source_snapshot:
            self.source_snapshot.close()
        self.project_snapshot.close()


@dataclass(slots=True)
class _RecipeInputs:
    """Small JSON inputs plus one current live-source identity receipt."""

    project_path: Path
    project_snapshot: _FileSnapshot
    project: Project
    source_path: Path
    source: Any
    source_path_receipt: FileReceipt
    scan_path: Path
    scan_snapshot: _FileSnapshot
    scan: dict[str, Any]

    def assert_unchanged(self) -> None:
        self.project_snapshot.assert_unchanged()
        self.scan_snapshot.assert_unchanged()
        try:
            current = FileReceipt.from_stat(
                self.source_path.stat(), self.source_path_receipt.sha256
            )
        except OSError as exc:
            raise GrooveSerpentError(
                "The source changed while the restoration recipe was created."
            ) from exc
        if current != self.source_path_receipt:
            raise GrooveSerpentError(
                "The source changed while the restoration recipe was created."
            )

    def close(self) -> None:
        self.scan_snapshot.close()
        self.project_snapshot.close()


class _PcmSpec:
    def __init__(self, bits: int) -> None:
        self.dtype: np.dtype[Any]
        if bits == 16:
            self.dtype = np.dtype("<i2")
            self.format = "s16le"
            self.codec = "pcm_s16le"
            self.sample_fmt = "s16"
        elif bits == 24:
            # FFmpeg exposes 24-bit integer PCM left-justified in signed s32.
            # Preview repair explicitly requantizes changed samples to multiples
            # of 256, and round-trip tests verify the assumption at runtime.
            self.dtype = np.dtype("<i4")
            self.format = "s32le"
            self.codec = "pcm_s32le"
            self.sample_fmt = "s32"
        else:
            raise GrooveSerpentError(
                "Click restoration currently supports integer 16-bit or 24-bit FLAC only."
            )
        self.bits = bits
        self.bytes_per_sample = self.dtype.itemsize


def _restoration_storage_required_bytes(
    *,
    source_size_bytes: int,
    music_frame_count: int,
    channels: int,
    bits_per_sample: int,
) -> int:
    """Return a conservative, integer-only render storage requirement."""

    values = {
        "source size": source_size_bytes,
        "music frame count": music_frame_count,
        "channel count": channels,
        "bit depth": bits_per_sample,
    }
    for label, value in values.items():
        if type(value) is not int or value < 0:
            raise GrooveSerpentError(
                f"Restoration render {label} is outside the supported range."
            )
    if channels == 0 or bits_per_sample == 0:
        raise GrooveSerpentError(
            "Restoration render audio geometry is outside the supported range."
        )
    bytes_per_sample = (bits_per_sample + 7) // 8
    uncompressed_music_bytes = music_frame_count * channels * bytes_per_sample
    return (
        max(source_size_bytes, uncompressed_music_bytes)
        + RENDER_STORAGE_SLACK_BYTES
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _load_json(path: Path, schema: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise GrooveSerpentError(f"Restoration JSON is invalid: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != schema:
        raise GrooveSerpentError(f"Expected restoration schema {schema}.")
    return payload


def _atomic_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    overwrite: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary, path)
        elif os.name == "nt":
            # Windows rename is an atomic no-replace operation: unlike POSIX
            # rename, it raises when the destination already exists.
            try:
                os.rename(temporary, path)
            except FileExistsError as exc:
                raise GrooveSerpentError(
                    f"Refusing to replace existing restoration JSON: {path}"
                ) from exc
        else:
            # Same-directory hard-link commit is atomic and fails if the final
            # pathname exists. The temporary name is removed after the link.
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise GrooveSerpentError(
                    f"Refusing to replace existing restoration JSON: {path}"
                ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot_file(path: Path, *, workspace: Path, label: str) -> _FileSnapshot:
    live_path = path.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    receipt = capture_file_receipt(live_path, label=label)
    temporary = tempfile.TemporaryDirectory(
        prefix="groove-serpent-input-",
        dir=str(workspace),
    )
    try:
        suffix = "".join(live_path.suffixes) or ".input"
        snapshot_path = Path(temporary.name) / f"input{suffix}"
        snapshot_receipt = stage_verified_copy(
            live_path,
            snapshot_path,
            receipt,
            label=label,
        )
        return _FileSnapshot(
            live_path=live_path,
            path=snapshot_path,
            live_receipt=receipt,
            snapshot_receipt=snapshot_receipt,
            temporary_directory=temporary,
            label=label,
        )
    except BaseException:
        temporary.cleanup()
        raise


def _validated_source(
    project_path: Path,
    project: Project,
    source_snapshot: VerifiedAudioSnapshot,
) -> tuple[Path, Any, _PcmSpec]:
    source_path = resolve_source_path(project, project_path)
    if source_snapshot.live_path.resolve() != source_path.resolve():
        raise GrooveSerpentError(
            "The supplied source snapshot belongs to a different source path."
        )
    source_snapshot.assert_snapshot_unchanged()
    source_snapshot.assert_live_unchanged()
    current = probe_audio(source_snapshot.path, stored_path=source_path.name)
    expected = project.source
    if (
        not expected.sha256
        or source_snapshot.sha256.lower() != expected.sha256.lower()
        or source_snapshot.size_bytes != expected.size_bytes
        or current.sha256.lower() != expected.sha256.lower()
        or current.size_bytes != expected.size_bytes
        or current.sample_rate != expected.sample_rate
        or current.channels != expected.channels
        or current.sample_count != expected.sample_count
    ):
        raise GrooveSerpentError(
            "The source no longer matches this project; click work was refused."
        )
    if current.codec_name.casefold() != "flac" or source_path.suffix.casefold() != ".flac":
        raise GrooveSerpentError(
            "Click restoration currently accepts lossless FLAC sources only."
        )
    if current.sample_count is None:
        raise GrooveSerpentError("The FLAC source has no exact decoded sample count.")
    bits = current.bits_per_raw_sample
    if bits not in {16, 24}:
        raise GrooveSerpentError(
            "Click restoration requires a known 16-bit or 24-bit FLAC source."
        )
    return source_path, current, _PcmSpec(bits)


def _prepare_restoration_inputs(
    project_path: Path,
    *,
    workspace: Path,
    source_snapshot: VerifiedAudioSnapshot | None,
    scan_path: Path | None = None,
    recipe_path: Path | None = None,
) -> _RestorationInputs:
    project_input: _FileSnapshot | None = None
    scan_input: _FileSnapshot | None = None
    recipe_input: _FileSnapshot | None = None
    operation_source: VerifiedAudioSnapshot | None = None
    owns_source_snapshot = False
    try:
        project_input = _snapshot_file(
            project_path,
            workspace=workspace,
            label="Project file",
        )
        project = load_project(project_input.path)
        live_source_path = resolve_source_path(project, project_path)
        if source_snapshot is None:
            snapshot_cache = resolve_cache_root(project_path)
            operation_source = verified_audio_snapshot(
                live_source_path,
                expected_sha256=project.source.sha256,
                expected_size_bytes=project.source.size_bytes,
                workspace=snapshot_cache,
                label="Source audio",
            )
            owns_source_snapshot = True
        else:
            operation_source = source_snapshot
        source_path, source, spec = _validated_source(
            project_path,
            project,
            operation_source,
        )

        scan: dict[str, Any] | None = None
        if scan_path is not None:
            scan_input = _snapshot_file(
                scan_path,
                workspace=workspace,
                label="Click scan",
            )
            scan = _load_json(scan_input.path, SCAN_SCHEMA)

        recipe: dict[str, Any] | None = None
        if recipe_path is not None:
            recipe_input = _snapshot_file(
                recipe_path,
                workspace=workspace,
                label="Restoration recipe",
            )
            recipe = _load_json(recipe_input.path, RECIPE_SCHEMA)

        return _RestorationInputs(
            project_path=project_path,
            project_snapshot=project_input,
            project=project,
            source_path=source_path,
            source_snapshot=operation_source,
            source=source,
            spec=spec,
            owns_source_snapshot=owns_source_snapshot,
            scan_path=scan_path,
            scan_snapshot=scan_input,
            scan=scan,
            recipe_path=recipe_path,
            recipe_snapshot=recipe_input,
            recipe=recipe,
        )
    except BaseException:
        if recipe_input is not None:
            recipe_input.close()
        if scan_input is not None:
            scan_input.close()
        if owns_source_snapshot and operation_source is not None:
            operation_source.close()
        if project_input is not None:
            project_input.close()
        raise


def _prepare_recipe_inputs(
    project_path: Path,
    scan_path: Path,
    *,
    workspace: Path,
    source_snapshot: VerifiedAudioSnapshot | None,
) -> _RecipeInputs:
    """Bind JSON recipe inputs without copying or probing album-sized audio."""

    project_input: _FileSnapshot | None = None
    scan_input: _FileSnapshot | None = None
    try:
        project_input = _snapshot_file(
            project_path,
            workspace=workspace,
            label="Project file",
        )
        project = load_project(project_input.path)
        source_path = resolve_source_path(project, project_path).resolve()
        expected = project.source
        if (
            not expected.sha256
            or expected.codec_name.casefold() != "flac"
            or source_path.suffix.casefold() != ".flac"
            or expected.sample_count is None
            or expected.bits_per_raw_sample not in {16, 24}
        ):
            raise GrooveSerpentError(
                "Click restoration requires a hash-bound 16-bit or 24-bit FLAC source."
            )
        live_receipt = capture_file_receipt(source_path, label="Source audio")
        if (
            live_receipt.sha256.lower() != expected.sha256.lower()
            or live_receipt.size_bytes != expected.size_bytes
        ):
            raise GrooveSerpentError(
                "The source no longer matches this project; recipe creation was refused."
            )
        if source_snapshot is not None and (
            source_snapshot.live_path.resolve() != source_path
            or source_snapshot.sha256.lower() != expected.sha256.lower()
            or source_snapshot.size_bytes != expected.size_bytes
        ):
            raise GrooveSerpentError(
                "The supplied source snapshot belongs to a different project source."
            )
        # Keep a path-stat receipt for a cheap final identity check.  The full
        # content hash was already captured through a stable open handle above.
        source_path_receipt = FileReceipt.from_stat(
            source_path.stat(), live_receipt.sha256
        )
        if not live_receipt.same_file_object(source_path_receipt):
            raise GrooveSerpentError(
                "The source changed while its recipe identity was captured."
            )
        scan_input = _snapshot_file(
            scan_path,
            workspace=workspace,
            label="Click scan",
        )
        scan = _load_json(scan_input.path, SCAN_SCHEMA)
        return _RecipeInputs(
            project_path=project_path,
            project_snapshot=project_input,
            project=project,
            source_path=source_path,
            source=expected,
            source_path_receipt=source_path_receipt,
            scan_path=scan_path,
            scan_snapshot=scan_input,
            scan=scan,
        )
    except BaseException:
        if scan_input is not None:
            scan_input.close()
        if project_input is not None:
            project_input.close()
        raise


def _decode_chunks(
    source_path: Path,
    *,
    sample_rate: int,
    channels: int,
    spec: _PcmSpec,
    start_frame: int,
    end_frame: int,
    chunk_frames: int = 262_144,
) -> Generator[np.ndarray, None, None]:
    ffmpeg = find_tool("ffmpeg")
    filter_graph = (
        f"atrim=start_sample={start_frame}:end_sample={end_frame},"
        "asetpts=PTS-STARTPTS"
    )
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source_path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-af",
        filter_graph,
        "-c:a",
        spec.codec,
        "-f",
        spec.format,
        "pipe:1",
    ]
    frame_bytes = channels * spec.bytes_per_sample
    process: subprocess.Popen[bytes] | None = None
    stderr_thread: threading.Thread | None = None
    diagnostic_capture: BoundedDiagnostic | None = None
    decoded_frames = 0
    remainder = b""
    completed = False
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            raise GrooveSerpentError("FFmpeg did not expose the restoration PCM pipe.")
        diagnostic_capture, stderr_thread = start_diagnostic_reader(
            process,
            name="groove-serpent-restoration-stderr",
        )
        while True:
            chunk = process.stdout.read(chunk_frames * frame_bytes)
            if not chunk:
                break
            chunk = remainder + chunk
            usable = len(chunk) - (len(chunk) % frame_bytes)
            remainder = chunk[usable:]
            if not usable:
                continue
            frames = (
                np.frombuffer(chunk[:usable], dtype=spec.dtype)
                .reshape(-1, channels)
                .copy()
            )
            decoded_frames += frames.shape[0]
            yield frames
        process.stdout.close()
        return_code = process.wait()
        join_diagnostic_reader(process, stderr_thread)
        diagnostic = diagnostic_capture.text() if diagnostic_capture else ""
        if return_code != 0:
            raise GrooveSerpentError(
                "FFmpeg failed while decoding restoration PCM"
                + (f": {diagnostic}" if diagnostic else ".")
            )
        if remainder:
            raise GrooveSerpentError("FFmpeg returned an incomplete PCM frame.")
        expected_frames = end_frame - start_frame
        if decoded_frames != expected_frames:
            raise GrooveSerpentError(
                f"FFmpeg decoded {decoded_frames} frames; expected {expected_frames}."
            )
        completed = True
    finally:
        if process is not None and process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
        if not completed:
            terminate_and_reap(process)
        join_diagnostic_reader(process, stderr_thread)


def _decode_array(
    source_path: Path,
    *,
    sample_rate: int,
    channels: int,
    spec: _PcmSpec,
    start_frame: int,
    end_frame: int,
) -> np.ndarray:
    parts = list(
        _decode_chunks(
            source_path,
            sample_rate=sample_rate,
            channels=channels,
            spec=spec,
            start_frame=start_frame,
            end_frame=end_frame,
            chunk_frames=max(1, end_frame - start_frame),
        )
    )
    if not parts:
        raise GrooveSerpentError("No PCM frames were decoded for the preview.")
    return parts[0] if len(parts) == 1 else np.concatenate(parts)


def _candidate_record(
    *,
    source_sha256: str,
    kind: str,
    interval: ClickInterval,
    absolute_start: int,
    sample_count: int,
    sample_rate: int,
) -> dict[str, Any]:
    detected_start = absolute_start + interval.start_sample
    detected_end = absolute_start + interval.end_sample
    padding = CLIP_REPAIR_PADDING_SAMPLES if kind == "clipped" else 0
    repair_start = max(0, detected_start - padding)
    repair_end = min(sample_count, detected_end + padding)
    repairable = (
        repair_start >= 1
        and repair_end < sample_count
        and repair_start <= absolute_start + interval.peak_sample < repair_end
        and repair_end > repair_start
        and repair_end - repair_start <= MAX_REPAIR_SAMPLES
    )
    candidate_id = _candidate_identifier(
        source_sha256=source_sha256,
        kind=kind,
        start_frame=repair_start,
        end_frame=repair_end,
        peak_frame=absolute_start + interval.peak_sample,
        channels=interval.channels,
    )
    return {
        "id": candidate_id,
        "type": kind,
        "detected_start_frame": detected_start,
        "detected_end_frame_exclusive": detected_end,
        "start_frame": repair_start,
        "end_frame_exclusive": repair_end,
        "peak_frame": absolute_start + interval.peak_sample,
        "channels": list(interval.channels),
        "confidence": round(float(interval.confidence), 8),
        "repairable": repairable,
        "start_seconds": repair_start / sample_rate,
        "end_seconds": repair_end / sample_rate,
    }


def _candidate_identifier(
    *,
    source_sha256: str,
    kind: str,
    start_frame: int,
    end_frame: int,
    peak_frame: int,
    channels: tuple[int, ...],
) -> str:
    identity = {
        "source_sha256": source_sha256,
        "detector": DETECTOR_NAME,
        "type": kind,
        "start_frame": start_frame,
        "end_frame_exclusive": end_frame,
        "peak_frame": peak_frame,
        "channels": list(channels),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return f"clk-{hashlib.sha256(encoded).hexdigest()[:20]}"


def _exclude_impulses_overlapping_clips(
    impulses: list[ClickInterval],
    clips: list[ClickInterval],
    channel_count: int,
    *,
    clip_padding_samples: int = 0,
) -> list[ClickInterval]:
    """Suppress duplicate evidence with a linear per-channel interval sweep."""

    clips_by_channel: list[list[ClickInterval]] = [
        [] for _ in range(channel_count)
    ]
    for clip in clips:
        for channel in clip.channels:
            clips_by_channel[channel].append(clip)
    positions = [0] * channel_count
    retained: list[ClickInterval] = []
    for impulse in impulses:
        overlaps = False
        for channel in impulse.channels:
            channel_clips = clips_by_channel[channel]
            position = positions[channel]
            while (
                position < len(channel_clips)
                and channel_clips[position].end_sample + clip_padding_samples
                <= impulse.start_sample
            ):
                position += 1
            positions[channel] = position
            if (
                position < len(channel_clips)
                and channel_clips[position].start_sample - clip_padding_samples
                < impulse.end_sample
            ):
                overlaps = True
                break
        if not overlaps:
            retained.append(impulse)
    return retained


def _detector_manifest() -> dict[str, Any]:
    return {
        "name": DETECTOR_NAME,
        "impulse_threshold_sigma": 10.0,
        "impulse_min_confidence": 0.15,
        "clip_threshold_ratio": 0.9999,
        "clip_repair_padding_samples": CLIP_REPAIR_PADDING_SAMPLES,
        "maximum_repair_frames": MAX_REPAIR_SAMPLES,
    }


def _require_exact_keys(
    value: Any, expected: set[str], label: str
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise GrooveSerpentError(
            f"{label} must be an object containing exactly: "
            + ", ".join(sorted(expected))
            + "."
        )
    return value


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validated_scan_candidates(
    scan: Mapping[str, Any],
    *,
    project_sha256: str,
    source: Any,
) -> dict[str, dict[str, Any]]:
    scan_project = scan.get("project")
    scan_source = scan.get("source")
    if not isinstance(scan_project, dict) or not isinstance(scan_source, dict):
        raise GrooveSerpentError("The click scan has invalid source or project binding.")
    if scan_project.get("sha256") != project_sha256:
        raise GrooveSerpentError("The project changed after this click scan.")
    expected_source = {
        "sha256": source.sha256,
        "size_bytes": source.size_bytes,
        "sample_rate": source.sample_rate,
        "channels": source.channels,
        "bits_per_raw_sample": source.bits_per_raw_sample,
        "sample_count": source.sample_count,
    }
    for key, expected in expected_source.items():
        observed = scan_source.get(key)
        if type(observed) is not type(expected) or observed != expected:
            raise GrooveSerpentError(
                f"The click scan has an invalid or stale source {key} binding."
            )
    detector = scan.get("detector")
    if type(detector) is not dict or detector != _detector_manifest():
        raise GrooveSerpentError(
            "The click scan uses unsupported or incomplete detector parameters."
        )
    candidates = scan.get("candidates")
    if type(candidates) is not list:
        raise GrooveSerpentError("The click scan has no valid candidate list.")

    candidate_keys = {
        "id",
        "type",
        "detected_start_frame",
        "detected_end_frame_exclusive",
        "start_frame",
        "end_frame_exclusive",
        "peak_frame",
        "channels",
        "confidence",
        "repairable",
        "start_seconds",
        "end_seconds",
    }
    validated: dict[str, dict[str, Any]] = {}
    assert source.sample_count is not None
    for raw_candidate in candidates:
        candidate = _require_exact_keys(
            raw_candidate, candidate_keys, "Each click candidate"
        )
        integer_keys = (
            "detected_start_frame",
            "detected_end_frame_exclusive",
            "start_frame",
            "end_frame_exclusive",
            "peak_frame",
        )
        if any(type(candidate.get(key)) is not int for key in integer_keys):
            raise GrooveSerpentError("A click candidate contains invalid sample bounds.")
        detected_start = candidate["detected_start_frame"]
        detected_end = candidate["detected_end_frame_exclusive"]
        start = candidate["start_frame"]
        end = candidate["end_frame_exclusive"]
        peak = candidate["peak_frame"]
        if (
            detected_start < 0
            or detected_end <= detected_start
            or detected_end > source.sample_count
            or start < 0
            or end <= start
            or end > source.sample_count
            or not start <= peak < end
        ):
            raise GrooveSerpentError("A click candidate contains unsafe sample bounds.")
        raw_channels = candidate.get("channels")
        if type(raw_channels) is not list or any(
            type(channel) is not int for channel in raw_channels
        ):
            raise GrooveSerpentError("A click candidate contains invalid channels.")
        channels = tuple(raw_channels)
        if channels != tuple(sorted(set(channels))) or not channels or any(
            channel < 0 or channel >= source.channels for channel in channels
        ):
            raise GrooveSerpentError("A click candidate contains invalid channels.")
        kind = candidate.get("type")
        if kind not in {"clipped", "impulse"}:
            raise GrooveSerpentError("A click candidate contains an invalid type.")
        confidence = candidate.get("confidence")
        try:
            rendered_confidence = strict_finite_number(
                confidence, "Click candidate confidence"
            )
        except ProjectValidationError:
            raise GrooveSerpentError(
                "A click candidate contains invalid confidence."
            ) from None
        if not 0.0 <= rendered_confidence <= 1.0:
            raise GrooveSerpentError("A click candidate contains invalid confidence.")
        expected_repairable = (
            start >= 1
            and end < source.sample_count
            and end - start <= MAX_REPAIR_SAMPLES
        )
        if type(candidate.get("repairable")) is not bool or (
            candidate["repairable"] != expected_repairable
        ):
            raise GrooveSerpentError("A click candidate has an invalid repairable flag.")
        for key, expected_seconds in (
            ("start_seconds", start / source.sample_rate),
            ("end_seconds", end / source.sample_rate),
        ):
            observed = candidate.get(key)
            try:
                rendered_observed = strict_finite_number(
                    observed, f"Click candidate {key}"
                )
            except ProjectValidationError:
                raise GrooveSerpentError(
                    "A click candidate has invalid time/sample correspondence."
                ) from None
            if abs(rendered_observed - expected_seconds) > 1e-9:
                raise GrooveSerpentError(
                    "A click candidate has invalid time/sample correspondence."
                )
        expected_id = _candidate_identifier(
            source_sha256=source.sha256,
            kind=kind,
            start_frame=start,
            end_frame=end,
            peak_frame=peak,
            channels=channels,
        )
        candidate_id = candidate.get("id")
        if candidate_id != expected_id:
            raise GrooveSerpentError("A click candidate was edited after detection.")
        if candidate_id in validated:
            raise GrooveSerpentError("A click candidate ID is duplicated in the scan.")
        validated[candidate_id] = candidate
    return validated


def _restoration_coverage(
    scan: Mapping[str, Any],
    project: Any,
) -> dict[str, Any]:
    """Return and validate the exact music-range coverage of a click scan."""

    scan_range = _require_exact_keys(
        scan.get("scan"),
        {"start_frame", "end_frame_exclusive", "start_seconds", "end_seconds"},
        "The click-scan range",
    )
    scan_start = scan_range["start_frame"]
    scan_end = scan_range["end_frame_exclusive"]
    if (
        type(scan_start) is not int
        or type(scan_end) is not int
        or scan_start < 0
        or scan_end <= scan_start
    ):
        raise GrooveSerpentError("The click scan has an invalid coverage range.")
    summary = scan.get("summary")
    if type(summary) is not dict:
        raise GrooveSerpentError("The click scan has no valid coverage summary.")
    detected = summary.get("detected")
    retained = summary.get("retained")
    truncated = summary.get("truncated")
    if (
        type(detected) is not int
        or type(retained) is not int
        or type(truncated) is not bool
        or detected < 0
        or retained < 0
        or retained > detected
        or truncated != (detected > retained)
    ):
        raise GrooveSerpentError("The click scan has an inconsistent coverage summary.")

    music_start = project.tracks[0].start_sample
    music_end = project.tracks[-1].end_sample
    music_frames = music_end - music_start
    covered_start = max(scan_start, music_start)
    covered_end = min(scan_end, music_end)
    covered_frames = max(0, covered_end - covered_start)
    unreviewed: list[dict[str, int]] = []
    if scan_start > music_start:
        end = min(scan_start, music_end)
        if end > music_start:
            unreviewed.append(
                {"start_frame": music_start, "end_frame_exclusive": end}
            )
    if scan_end < music_end:
        start = max(scan_end, music_start)
        if music_end > start:
            unreviewed.append(
                {"start_frame": start, "end_frame_exclusive": music_end}
            )
    covers_music = scan_start <= music_start and scan_end >= music_end
    complete = covers_music and not truncated
    status = "complete" if complete else ("partial" if covered_frames else "exploratory")
    ledger = {
        "music_start_frame": music_start,
        "music_end_frame_exclusive": music_end,
        "music_frame_count": music_frames,
        "scanned_music_frames": covered_frames,
        "scanned_music_percent": covered_frames * 100.0 / music_frames,
        "scan_range_covers_music": covers_music,
        "candidate_scan_truncated": truncated,
        "detected_candidates": detected,
        "retained_candidates": retained,
        "unretained_detections": detected - retained,
        "unreviewed_regions": unreviewed,
        "restoration_status": status,
    }
    embedded = scan.get("coverage")
    if embedded is not None and (type(embedded) is not dict or embedded != ledger):
        raise GrooveSerpentError("The click scan coverage ledger was edited or is stale.")
    return ledger


def _validate_recipe_payload(
    recipe: Mapping[str, Any],
    *,
    project_path: Path,
    project_sha256: str,
    source_path: Path,
    source_sha256: str,
    scan_path: Path,
    scan_sha256: str,
    candidates: Mapping[str, Mapping[str, Any]],
    expected_coverage: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    recipe_keys = {
        "schema",
        "created_at",
        "app_version",
        "project",
        "source",
        "scan",
        "backend",
        "decisions",
        "summary",
    }
    if "coverage" in recipe:
        recipe_keys.add("coverage")
    top = _require_exact_keys(
        recipe,
        recipe_keys,
        "The restoration recipe",
    )
    if top["schema"] != RECIPE_SCHEMA:
        raise GrooveSerpentError(f"Expected restoration schema {RECIPE_SCHEMA}.")
    if any(
        not isinstance(top[key], str) or not top[key] or len(top[key]) > 200
        for key in ("created_at", "app_version")
    ):
        raise GrooveSerpentError("The restoration recipe has invalid provenance text.")
    expected_bindings = (
        ("project", project_path.name, project_sha256),
        ("source", source_path.name, source_sha256),
        ("scan", scan_path.name, scan_sha256),
    )
    for label, expected_path, expected_hash in expected_bindings:
        binding = _require_exact_keys(
            top[label], {"path", "sha256"}, f"The recipe {label} binding"
        )
        if (
            binding["path"] != expected_path
            or not _is_sha256(binding["sha256"])
            or binding["sha256"] != expected_hash
        ):
            raise GrooveSerpentError(
                f"The restoration recipe belongs to a different {label}."
            )
    backend = _require_exact_keys(
        top["backend"],
        {"name", "maximum_repair_frames"},
        "The recipe backend",
    )
    if backend != {
        "name": REPAIR_BACKEND,
        "maximum_repair_frames": MAX_REPAIR_SAMPLES,
    }:
        raise GrooveSerpentError("The restoration recipe uses an unsupported backend.")
    decisions = top["decisions"]
    if type(decisions) is not list:
        raise GrooveSerpentError("Recipe decisions must be an array.")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_decision in decisions:
        if type(raw_decision) is not dict:
            raise GrooveSerpentError("Each recipe decision must be an object.")
        decision_value = raw_decision.get("decision")
        expected_keys = {"candidate_id", "decision"}
        if decision_value == "protected":
            expected_keys.add("classification")
        decision = _require_exact_keys(
            raw_decision, expected_keys, "Each recipe decision"
        )
        candidate_id = decision["candidate_id"]
        if not isinstance(candidate_id, str) or candidate_id not in candidates:
            raise GrooveSerpentError("A recipe decision has an unknown candidate ID.")
        if candidate_id in seen:
            raise GrooveSerpentError("A recipe candidate decision is duplicated.")
        seen.add(candidate_id)
        if decision_value not in {"approved", "rejected", "protected"}:
            raise GrooveSerpentError("A recipe decision has an invalid decision value.")
        if decision_value == "approved" and candidates[candidate_id].get(
            "repairable"
        ) is not True:
            raise GrooveSerpentError("A non-repairable candidate cannot be approved.")
        if decision_value == "protected" and decision.get(
            "classification"
        ) not in _PROTECTED_CLASSIFICATIONS:
            raise GrooveSerpentError(
                "A protected candidate needs an explicit structural classification."
            )
        normalized.append(dict(decision))
    if seen != set(candidates):
        raise GrooveSerpentError(
            "The restoration recipe must decide every retained scan candidate exactly once."
        )
    summary = _require_exact_keys(
        top["summary"],
        {"candidates", "approved", "rejected", "protected"},
        "The recipe summary",
    )
    expected_summary = {
        "candidates": len(normalized),
        "approved": sum(item["decision"] == "approved" for item in normalized),
        "rejected": sum(item["decision"] == "rejected" for item in normalized),
        "protected": sum(item["decision"] == "protected" for item in normalized),
    }
    if any(type(value) is not int for value in summary.values()) or summary != expected_summary:
        raise GrooveSerpentError("The restoration recipe summary is inconsistent.")
    if expected_coverage is not None and "coverage" in top:
        if type(top["coverage"]) is not dict or top["coverage"] != dict(expected_coverage):
            raise GrooveSerpentError(
                "The restoration recipe has an invalid or stale coverage ledger."
            )
    return normalized


def scan_project_clicks(
    project_path: Path | str,
    report_path: Path | str,
    *,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    max_candidates: int = 500,
    source_snapshot: VerifiedAudioSnapshot | None = None,
) -> dict[str, Any]:
    """Scan exact source PCM and atomically write a review-only candidate report."""

    project_path = Path(project_path).expanduser().resolve()
    report_path = Path(report_path).expanduser().resolve()
    if report_path.exists():
        raise GrooveSerpentError(f"Click-scan report already exists: {report_path}")
    inputs = _prepare_restoration_inputs(
        project_path,
        workspace=report_path.parent,
        source_snapshot=source_snapshot,
    )
    try:
        return _scan_project_clicks(
            inputs,
            report_path,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            max_candidates=max_candidates,
        )
    finally:
        inputs.close()


def _scan_project_clicks(
    inputs: _RestorationInputs,
    report_path: Path,
    *,
    start_seconds: float | None,
    end_seconds: float | None,
    max_candidates: int,
) -> dict[str, Any]:
    project_path = inputs.project_path
    project = inputs.project
    source_path = inputs.source_path
    source = inputs.source
    spec = inputs.spec
    initial_project_sha256 = inputs.project_snapshot.live_receipt.sha256
    protected = {project_path, source_path.resolve()}
    if report_path in protected:
        raise GrooveSerpentError("The click-scan report cannot replace the project or source.")
    if (
        isinstance(max_candidates, bool)
        or not isinstance(max_candidates, int)
        or max_candidates < 1
    ):
        raise GrooveSerpentError("max_candidates must be a positive integer.")
    if max_candidates > 10_000:
        raise GrooveSerpentError("max_candidates may not exceed 10000.")

    def seconds_to_frame(value: float | None, default: int) -> int:
        if value is None:
            return default
        try:
            rendered = strict_finite_number(value, "Scan start/end time")
        except ProjectValidationError as exc:
            raise GrooveSerpentError(
                "Scan start/end times must be finite numbers."
            ) from exc
        scaled = rendered * source.sample_rate
        if not math.isfinite(scaled):
            raise GrooveSerpentError("Scan start/end times are outside the supported range.")
        return int(round(scaled))

    assert source.sample_count is not None
    scan_start = max(0, seconds_to_frame(start_seconds, 0))
    scan_end = min(source.sample_count, seconds_to_frame(end_seconds, source.sample_count))
    if scan_end - scan_start < 256:
        raise GrooveSerpentError("The click-scan range must contain at least 256 frames.")

    retained: list[tuple[tuple[Any, ...], int, dict[str, Any]]] = []
    detected_total = 0
    overlap = 1_024
    decode_start = max(0, scan_start - overlap)
    decode_end = min(source.sample_count, scan_end + overlap)
    tail = np.empty((0, source.channels), dtype=spec.dtype)
    consumed = 0
    first = True

    def collect(buffer: np.ndarray, buffer_start: int, accept_start: int, accept_end: int) -> None:
        nonlocal detected_total
        clipped = detect_clipped_runs(buffer)
        raw_impulses = [
            item
            for item in detect_impulsive_clicks(buffer)
            if item.confidence >= 0.15
        ]
        impulses = _exclude_impulses_overlapping_clips(
            raw_impulses,
            clipped,
            source.channels,
            clip_padding_samples=CLIP_REPAIR_PADDING_SAMPLES,
        )
        found = [("clipped", item) for item in clipped] + [
            ("impulse", item) for item in impulses
        ]
        for kind, interval in found:
            if not accept_start <= interval.peak_sample < accept_end:
                continue
            absolute_peak = buffer_start + interval.peak_sample
            if not scan_start <= absolute_peak < scan_end:
                continue
            record = _candidate_record(
                source_sha256=source.sha256,
                kind=kind,
                interval=interval,
                absolute_start=buffer_start,
                sample_count=source.sample_count,
                sample_rate=source.sample_rate,
            )
            detected_total += 1
            quality = (
                1 if record["type"] == "clipped" else 0,
                float(record["confidence"]),
                -int(record["start_frame"]),
                str(record["id"]),
            )
            heapq.heappush(retained, (quality, detected_total, record))
            if len(retained) > max_candidates:
                heapq.heappop(retained)

    for chunk in _decode_chunks(
        inputs.source_snapshot.path,
        sample_rate=source.sample_rate,
        channels=source.channels,
        spec=spec,
        start_frame=decode_start,
        end_frame=decode_end,
    ):
        buffer = np.concatenate((tail, chunk)) if tail.size else chunk
        buffer_start = decode_start + consumed - tail.shape[0]
        half_overlap = min(overlap // 2, buffer.shape[0] // 2)
        accept_start = 0 if first else half_overlap
        accept_end = buffer.shape[0] - half_overlap
        if accept_end > accept_start:
            collect(buffer, buffer_start, accept_start, accept_end)
        tail = buffer[-min(overlap, buffer.shape[0]) :].copy()
        consumed += chunk.shape[0]
        first = False
    if tail.size:
        buffer_start = decode_end - tail.shape[0]
        collect(tail, buffer_start, min(overlap // 2, tail.shape[0]), tail.shape[0])

    records = [entry[2] for entry in retained]
    records.sort(key=lambda item: (int(item["start_frame"]), str(item["id"])))

    if report_path.exists():
        raise GrooveSerpentError(f"Click-scan report already exists: {report_path}")

    report = {
        "schema": SCAN_SCHEMA,
        "created_at": utc_now_iso(),
        "app_version": __version__,
        "project": {
            "path": project_path.name,
            "sha256": initial_project_sha256,
        },
        "source": {
            "path": source_path.name,
            "sha256": source.sha256,
            "size_bytes": source.size_bytes,
            "sample_rate": source.sample_rate,
            "channels": source.channels,
            "bits_per_raw_sample": source.bits_per_raw_sample,
            "sample_count": source.sample_count,
        },
        "decoder": {
            "ffmpeg": tool_version("ffmpeg"),
            "canonical_pcm": f"{spec.format}-interleaved",
            "bytes_per_frame": spec.bytes_per_sample * source.channels,
            "immutable_source_snapshot": True,
            "source_snapshot_sha256": inputs.source_snapshot.sha256,
        },
        "detector": _detector_manifest(),
        "scan": {
            "start_frame": scan_start,
            "end_frame_exclusive": scan_end,
            "start_seconds": scan_start / source.sample_rate,
            "end_seconds": scan_end / source.sample_rate,
        },
        "candidates": records,
        "summary": {
            "detected": detected_total,
            "retained": len(records),
            "truncated": detected_total > len(records),
            "clipped": sum(item["type"] == "clipped" for item in records),
            "impulse": sum(item["type"] == "impulse" for item in records),
            "repairable": sum(bool(item["repairable"]) for item in records),
        },
    }
    report["coverage"] = _restoration_coverage(report, project)
    inputs.assert_unchanged()
    if report_path.exists():
        raise GrooveSerpentError(f"Click-scan report already exists: {report_path}")
    _atomic_json(report_path, report, overwrite=False)
    return report


def create_restoration_recipe(
    project_path: Path | str,
    scan_path: Path | str,
    decisions: Sequence[Mapping[str, Any]],
    recipe_path: Path | str,
    *,
    source_snapshot: VerifiedAudioSnapshot | None = None,
) -> dict[str, Any]:
    """Bind one explicit decision to every retained scan candidate."""

    project_path = Path(project_path).expanduser().resolve()
    scan_path = Path(scan_path).expanduser().resolve()
    recipe_path = Path(recipe_path).expanduser().resolve()
    if recipe_path.exists():
        raise GrooveSerpentError(
            f"Restoration recipe already exists: {recipe_path}"
        )
    inputs = _prepare_recipe_inputs(
        project_path,
        scan_path,
        workspace=recipe_path.parent,
        source_snapshot=source_snapshot,
    )
    try:
        return _create_restoration_recipe(inputs, decisions, recipe_path)
    finally:
        inputs.close()


def _create_restoration_recipe(
    inputs: _RecipeInputs,
    decisions: Sequence[Mapping[str, Any]],
    recipe_path: Path,
) -> dict[str, Any]:
    project_path = inputs.project_path
    project = inputs.project
    source_path = inputs.source_path
    source = inputs.source
    scan_path = inputs.scan_path
    scan = inputs.scan
    scan_snapshot = inputs.scan_snapshot
    initial_project_sha256 = inputs.project_snapshot.live_receipt.sha256
    initial_scan_sha256 = scan_snapshot.live_receipt.sha256
    candidates = _validated_scan_candidates(
        scan,
        project_sha256=initial_project_sha256,
        source=source,
    )
    coverage = _restoration_coverage(scan, project)
    if (
        isinstance(decisions, (str, bytes, bytearray, Mapping))
        or not isinstance(decisions, Sequence)
        or any(type(item) is not dict for item in decisions)
    ):
        raise GrooveSerpentError("Recipe decisions must be an array of strict objects.")
    decision_list = [dict(item) for item in decisions]
    protected = {project_path, source_path.resolve(), scan_path}
    if recipe_path in protected:
        raise GrooveSerpentError("The restoration recipe cannot replace an input file.")
    recipe = {
        "schema": RECIPE_SCHEMA,
        "created_at": utc_now_iso(),
        "app_version": __version__,
        "project": {
            "path": project_path.name,
            "sha256": initial_project_sha256,
        },
        "source": {
            "path": source_path.name,
            "sha256": source.sha256,
        },
        "scan": {
            "path": scan_path.name,
            "sha256": initial_scan_sha256,
        },
        "backend": {
            "name": REPAIR_BACKEND,
            "maximum_repair_frames": MAX_REPAIR_SAMPLES,
        },
        "decisions": decision_list,
        "summary": {
            "candidates": len(decision_list),
            "approved": sum(
                item.get("decision") == "approved" for item in decision_list
            ),
            "rejected": sum(
                item.get("decision") == "rejected" for item in decision_list
            ),
            "protected": sum(
                item.get("decision") == "protected" for item in decision_list
            ),
        },
        "coverage": coverage,
    }
    _validate_recipe_payload(
        recipe,
        project_path=project_path,
        project_sha256=initial_project_sha256,
        source_path=source_path,
        source_sha256=source.sha256,
        scan_path=scan_path,
        scan_sha256=initial_scan_sha256,
        candidates=candidates,
        expected_coverage=coverage,
    )
    inputs.assert_unchanged()
    if recipe_path.exists():
        raise GrooveSerpentError(
            f"Restoration recipe already exists: {recipe_path}"
        )
    _atomic_json(recipe_path, recipe, overwrite=False)
    result = dict(recipe)
    result["recipe_path"] = str(recipe_path)
    return result


def _encode_flac(
    path: Path,
    audio: np.ndarray,
    *,
    sample_rate: int,
    channels: int,
    spec: _PcmSpec,
) -> None:
    ffmpeg = find_tool("ffmpeg")
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        spec.format,
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
        "-i",
        "pipe:0",
        "-map",
        "0:a:0",
        "-c:a",
        "flac",
        "-compression_level",
        "8",
        "-sample_fmt",
        spec.sample_fmt,
        str(path),
    ]
    payload = np.ascontiguousarray(audio, dtype=spec.dtype).tobytes()
    process: subprocess.Popen[bytes] | None = None
    stderr_thread: threading.Thread | None = None
    diagnostic_capture: BoundedDiagnostic | None = None
    completed = False
    write_error: OSError | None = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if process.stdin is None or process.stderr is None:
            raise GrooveSerpentError("FFmpeg did not expose the preview encoder pipe.")
        diagnostic_capture, stderr_thread = start_diagnostic_reader(
            process,
            name="groove-serpent-restoration-stderr",
        )
        try:
            process.stdin.write(payload)
        except OSError as exc:
            write_error = exc
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
        return_code = process.wait()
        join_diagnostic_reader(process, stderr_thread)
        diagnostic = diagnostic_capture.text() if diagnostic_capture else ""
        if return_code != 0:
            raise GrooveSerpentError(
                "FFmpeg could not encode the click preview"
                + (f": {diagnostic}" if diagnostic else ".")
            )
        if write_error is not None:
            raise GrooveSerpentError(
                f"FFmpeg preview PCM streaming failed: {write_error}"
            ) from write_error
        completed = True
    except OSError as exc:
        raise GrooveSerpentError(f"Could not start the preview encoder: {exc}") from exc
    finally:
        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if not completed:
            terminate_and_reap(process)
        join_diagnostic_reader(process, stderr_thread)


def _quantize_24_patch(values: np.ndarray) -> np.ndarray:
    wide = values.astype(np.int64, copy=False)
    magnitude = np.abs(wide)
    quantized = ((magnitude + 128) // 256) * 256
    quantized = np.where(wide < 0, -quantized, quantized)
    # Valid left-justified 24-bit PCM spans INT32_MIN through 0x7fffff00.
    # INT32_MAX itself has non-zero padding bits and cannot round-trip exactly.
    return np.asarray(
        np.clip(quantized, -(1 << 31), (1 << 31) - 256), dtype=np.int32
    )


def _removed_signal(
    before: np.ndarray,
    proposed: np.ndarray,
    spec: _PcmSpec,
) -> tuple[np.ndarray, int]:
    difference = before.astype(np.int64) - proposed.astype(np.int64)
    amplified = difference * int(REMOVED_SIGNAL_GAIN)
    if spec.bits == 24:
        minimum, maximum = -(1 << 31), (1 << 31) - 256
        clipped = int(np.count_nonzero((amplified < minimum) | (amplified > maximum)))
        return _quantize_24_patch(amplified), clipped
    limits = np.iinfo(np.int16)
    clipped = int(
        np.count_nonzero((amplified < limits.min) | (amplified > limits.max))
    )
    return np.clip(amplified, limits.min, limits.max).astype(np.int16), clipped


def _preview_metrics(
    audio: np.ndarray,
    repair_windows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    values = audio.astype(np.float64)
    curvature = values[1:-1] - (values[:-2] + values[2:]) / 2.0
    approved_values: list[np.ndarray] = []
    curvature_regions: list[np.ndarray] = []
    boundaries: list[dict[str, Any]] = []
    for window in repair_windows:
        start = int(window["start_in_preview"])
        end = int(window["end_in_preview_exclusive"])
        channels = [int(value) for value in window["channels"]]
        approved_values.append(values[start:end, channels].reshape(-1))
        region_start = max(0, start - 33)
        region_end = min(curvature.shape[0], end + 31)
        curvature_regions.append(
            curvature[region_start:region_end, channels].reshape(-1)
        )
        boundaries.append(
            {
                "candidate_id": window["candidate_id"],
                "channels": channels,
                "left_jump": [
                    int(abs(values[start, channel] - values[start - 1, channel]))
                    for channel in channels
                ],
                "right_jump": [
                    int(abs(values[end, channel] - values[end - 1, channel]))
                    for channel in channels
                ],
            }
        )
    approved = np.concatenate(approved_values)
    local_curvature = np.concatenate(curvature_regions)
    return {
        "approved_peak_absolute_sample": int(np.max(np.abs(approved))),
        "approved_local_curvature_rms": float(
            np.sqrt(np.mean(np.square(local_curvature)))
        ),
        "window_boundaries": boundaries,
    }


def create_click_preview(
    project_path: Path | str,
    scan_path: Path | str,
    candidate_id: str | Sequence[str],
    bundle_dir: Path | str,
    *,
    context_seconds: float = 2.0,
    source_snapshot: VerifiedAudioSnapshot | None = None,
) -> dict[str, Any]:
    """Create one lossless A/B event preview; never alter source or project."""

    project_path = Path(project_path).expanduser().resolve()
    scan_path = Path(scan_path).expanduser().resolve()
    bundle_dir = Path(bundle_dir).expanduser().resolve()
    if bundle_dir.exists():
        raise GrooveSerpentError(f"Preview bundle already exists: {bundle_dir}")
    inputs = _prepare_restoration_inputs(
        project_path,
        workspace=bundle_dir.parent,
        source_snapshot=source_snapshot,
        scan_path=scan_path,
    )
    try:
        return _create_click_preview(
            inputs,
            candidate_id,
            bundle_dir,
            context_seconds=context_seconds,
        )
    finally:
        inputs.close()


def _create_click_preview(
    inputs: _RestorationInputs,
    candidate_id: str | Sequence[str],
    bundle_dir: Path,
    *,
    context_seconds: float,
) -> dict[str, Any]:
    project_path = inputs.project_path
    source_path = inputs.source_path
    source = inputs.source
    spec = inputs.spec
    scan_path = inputs.scan_path
    scan = inputs.scan
    scan_snapshot = inputs.scan_snapshot
    if scan_path is None or scan is None or scan_snapshot is None:
        raise AssertionError("Click preview requires a snapshotted click scan.")
    initial_project_sha256 = inputs.project_snapshot.live_receipt.sha256
    initial_scan_sha256 = scan_snapshot.live_receipt.sha256
    if isinstance(candidate_id, str):
        candidate_ids = [candidate_id]
    elif isinstance(candidate_id, Sequence) and not isinstance(
        candidate_id, (bytes, bytearray)
    ):
        candidate_ids = list(candidate_id)
    else:
        candidate_ids = []
    if (
        not candidate_ids
        or len(candidate_ids) > MAX_PREVIEW_CANDIDATES
        or any(
            not isinstance(value, str) or not value.startswith("clk-")
            for value in candidate_ids
        )
        or len(set(candidate_ids)) != len(candidate_ids)
    ):
        raise GrooveSerpentError(
            f"Provide 1 to {MAX_PREVIEW_CANDIDATES} unique click candidate IDs."
        )
    try:
        rendered_context_seconds = strict_finite_number(
            context_seconds, "Preview context"
        )
    except ProjectValidationError as exc:
        raise GrooveSerpentError(
            "Preview context must be between 0.1 and 30 seconds."
        ) from exc
    if not 0.1 <= rendered_context_seconds <= 30.0:
        raise GrooveSerpentError("Preview context must be between 0.1 and 30 seconds.")

    scan_project = scan.get("project")
    scan_source = scan.get("source")
    scan_detector = scan.get("detector")
    if not isinstance(scan_project, dict) or not isinstance(scan_source, dict):
        raise GrooveSerpentError("The click scan has invalid source or project binding.")
    if not isinstance(scan_detector, dict) or scan_detector != _detector_manifest():
        raise GrooveSerpentError(
            "The click scan uses unsupported or incomplete detector parameters."
        )
    if scan_project.get("sha256") != initial_project_sha256:
        raise GrooveSerpentError("The project changed after this click scan.")
    if scan_source.get("sha256") != source.sha256:
        raise GrooveSerpentError("The click scan belongs to a different source file.")
    candidates = scan.get("candidates")
    if not isinstance(candidates, list):
        raise GrooveSerpentError("The click scan has no valid candidate list.")
    assert source.sample_count is not None
    selected: list[
        tuple[dict[str, Any], int, int, int, tuple[int, ...], float]
    ] = []
    for requested_id in candidate_ids:
        matches = [
            item
            for item in candidates
            if isinstance(item, dict) and item.get("id") == requested_id
        ]
        if len(matches) != 1:
            raise GrooveSerpentError(
                "A click candidate was not found uniquely in this scan."
            )
        candidate = matches[0]
        if candidate.get("repairable") is not True:
            raise GrooveSerpentError(
                "A candidate is too broad or too close to an edge to preview."
            )
        try:
            repair_start = candidate["start_frame"]
            repair_end = candidate["end_frame_exclusive"]
            peak_frame = candidate["peak_frame"]
            raw_channels = candidate["channels"]
            kind = candidate["type"]
            confidence = candidate["confidence"]
        except KeyError as exc:
            raise GrooveSerpentError(
                "A click candidate contains invalid sample bounds."
            ) from exc
        if any(
            type(value) is not int
            for value in (repair_start, repair_end, peak_frame)
        ):
            raise GrooveSerpentError(
                "A click candidate contains invalid sample bounds."
            )
        if not isinstance(raw_channels, list) or any(
            type(value) is not int for value in raw_channels
        ):
            raise GrooveSerpentError("A click candidate contains invalid channels.")
        channels = tuple(raw_channels)
        if channels != tuple(sorted(set(channels))) or not channels or any(
            channel < 0 or channel >= source.channels for channel in channels
        ):
            raise GrooveSerpentError("A click candidate contains invalid channels.")
        if kind not in {"clipped", "impulse"}:
            raise GrooveSerpentError("A click candidate contains an invalid type.")
        try:
            rendered_confidence = strict_finite_number(
                confidence, "Click candidate confidence"
            )
        except ProjectValidationError:
            raise GrooveSerpentError(
                "A click candidate contains invalid confidence."
            ) from None
        if not 0.0 <= rendered_confidence <= 1.0:
            raise GrooveSerpentError("A click candidate contains invalid confidence.")
        if (
            repair_start < 1
            or repair_end >= source.sample_count
            or repair_end <= repair_start
            or repair_end - repair_start > MAX_REPAIR_SAMPLES
            or not repair_start <= peak_frame < repair_end
        ):
            raise GrooveSerpentError(
                "A click candidate contains unsafe sample bounds."
            )
        expected_candidate_id = _candidate_identifier(
            source_sha256=source.sha256,
            kind=kind,
            start_frame=repair_start,
            end_frame=repair_end,
            peak_frame=peak_frame,
            channels=channels,
        )
        if requested_id != expected_candidate_id:
            raise GrooveSerpentError("A click candidate was edited after detection.")
        selected.append(
            (
                candidate,
                repair_start,
                repair_end,
                peak_frame,
                channels,
                float(confidence),
            )
        )

    selected.sort(key=lambda item: (item[1], item[2], item[4]))
    for index, left in enumerate(selected):
        for right in selected[index + 1 :]:
            if max(left[1], right[1]) <= min(left[2], right[2]) and not set(
                left[4]
            ).isdisjoint(right[4]):
                raise GrooveSerpentError(
                    "Selected candidate windows overlap or touch in the same channel."
                )
    repair_start = min(item[1] for item in selected)
    repair_end = max(item[2] for item in selected)
    if repair_end - repair_start > source.sample_rate:
        raise GrooveSerpentError(
            "Selected candidates must belong to the same event within one second."
        )

    context_frames = round(float(context_seconds) * source.sample_rate)
    context_start = max(0, repair_start - context_frames)
    context_end = min(source.sample_count, repair_end + context_frames)
    before = _decode_array(
        inputs.source_snapshot.path,
        sample_rate=source.sample_rate,
        channels=source.channels,
        spec=spec,
        start_frame=context_start,
        end_frame=context_end,
    )
    local_start = repair_start - context_start
    local_end = repair_end - context_start
    proposed = before.copy()
    allowed = np.zeros(before.shape, dtype=np.bool_)
    repair_windows: list[dict[str, Any]] = []
    for candidate, start, end, peak, channels, confidence in selected:
        window_start = start - context_start
        window_end = end - context_start
        interval = ClickInterval(
            start_sample=window_start,
            end_sample=window_end,
            peak_sample=peak - context_start,
            confidence=confidence,
            channels=channels,
        )
        independently_repaired = repair_click_intervals(before, [interval])
        for channel in channels:
            patch = independently_repaired[window_start:window_end, channel]
            if spec.bits == 24:
                patch = _quantize_24_patch(patch)
            proposed[window_start:window_end, channel] = patch
            allowed[window_start:window_end, channel] = True
        repair_windows.append(
            {
                "candidate_id": candidate["id"],
                "start_in_preview": window_start,
                "end_in_preview_exclusive": window_end,
                "channels": list(channels),
            }
        )
    if not np.array_equal(before[~allowed], proposed[~allowed]):
        raise GrooveSerpentError(
            "The proposed repair changed PCM outside its approved channels/window."
        )
    changed = int(np.count_nonzero(before[allowed] != proposed[allowed]))
    if changed == 0:
        raise GrooveSerpentError("The proposed repair did not change any candidate samples.")
    removed, removed_clipped = _removed_signal(before, proposed, spec)
    if np.count_nonzero(removed[~allowed]) != 0:
        raise GrooveSerpentError("The removed-signal preview leaked outside its approved window.")

    protected = {project_path, source_path.resolve(), scan_path}
    if bundle_dir in protected:
        raise GrooveSerpentError("The preview bundle cannot replace an input file.")
    bundle_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            dir=bundle_dir.parent,
            prefix=f".{bundle_dir.name}.",
            suffix=".partial",
        )
    ).resolve()
    committed = False
    try:
        before_path = stage / "before.flac"
        proposed_path = stage / "proposed.flac"
        removed_path = stage / "removed.flac"
        _encode_flac(
            before_path,
            before,
            sample_rate=source.sample_rate,
            channels=source.channels,
            spec=spec,
        )
        _encode_flac(
            proposed_path,
            proposed,
            sample_rate=source.sample_rate,
            channels=source.channels,
            spec=spec,
        )
        _encode_flac(
            removed_path,
            removed,
            sample_rate=source.sample_rate,
            channels=source.channels,
            spec=spec,
        )
        decoded_before = _decode_array(
            before_path,
            sample_rate=source.sample_rate,
            channels=source.channels,
            spec=spec,
            start_frame=0,
            end_frame=before.shape[0],
        )
        decoded_proposed = _decode_array(
            proposed_path,
            sample_rate=source.sample_rate,
            channels=source.channels,
            spec=spec,
            start_frame=0,
            end_frame=proposed.shape[0],
        )
        decoded_removed = _decode_array(
            removed_path,
            sample_rate=source.sample_rate,
            channels=source.channels,
            spec=spec,
            start_frame=0,
            end_frame=removed.shape[0],
        )
        if not np.array_equal(decoded_before, before) or not np.array_equal(
            decoded_proposed, proposed
        ) or not np.array_equal(decoded_removed, removed):
            raise GrooveSerpentError("Lossless preview round-trip verification failed.")
        if not np.array_equal(decoded_before[~allowed], decoded_proposed[~allowed]):
            raise GrooveSerpentError("Encoded previews differ outside the candidate window.")
        before_probe = probe_audio(before_path)
        proposed_probe = probe_audio(proposed_path)
        removed_probe = probe_audio(removed_path)
        expected_shape = (
            source.sample_rate,
            source.channels,
            source.bits_per_raw_sample,
            before.shape[0],
        )
        if (
            before_probe.sample_rate,
            before_probe.channels,
            before_probe.bits_per_raw_sample,
            before_probe.sample_count,
        ) != expected_shape or (
            proposed_probe.sample_rate,
            proposed_probe.channels,
            proposed_probe.bits_per_raw_sample,
            proposed_probe.sample_count,
        ) != expected_shape or (
            removed_probe.sample_rate,
            removed_probe.channels,
            removed_probe.bits_per_raw_sample,
            removed_probe.sample_count,
        ) != expected_shape:
            raise GrooveSerpentError("Preview audio format or frame count changed.")

        manifest = {
            "schema": PREVIEW_SCHEMA,
            "created_at": utc_now_iso(),
            "app_version": __version__,
            "source": {
                "path": source_path.name,
                "sha256": source.sha256,
                "sample_rate": source.sample_rate,
                "channels": source.channels,
                "bits_per_raw_sample": source.bits_per_raw_sample,
            },
            "scan": {
                "path": scan_path.name,
                "sha256": initial_scan_sha256,
            },
            "candidates": [item[0] for item in selected],
            "context": {
                "start_frame": context_start,
                "end_frame_exclusive": context_end,
                "repair_start_in_preview": local_start,
                "repair_end_in_preview_exclusive": local_end,
                "repair_windows": repair_windows,
            },
            "backend": {
                "name": REPAIR_BACKEND,
                "maximum_repair_frames": MAX_REPAIR_SAMPLES,
                "audacity_used": False,
                "immutable_source_snapshot": True,
                "source_snapshot_sha256": inputs.source_snapshot.sha256,
            },
            "files": {
                "before": {
                    "path": before_path.name,
                    "sha256": sha256_file(before_path),
                },
                "proposed": {
                    "path": proposed_path.name,
                    "sha256": sha256_file(proposed_path),
                },
                "removed": {
                    "path": removed_path.name,
                    "sha256": sha256_file(removed_path),
                },
            },
            "audition": {
                "before_linear_gain": 1.0,
                "proposed_linear_gain": 1.0,
                "removed_linear_gain": REMOVED_SIGNAL_GAIN,
                "removed_gain_db": 20.0 * math.log10(REMOVED_SIGNAL_GAIN),
                "definition": "removed = (before - proposed) * removed_linear_gain",
                "matched_original_level": True,
            },
            "metrics": {
                "before": _preview_metrics(before, repair_windows),
                "proposed": _preview_metrics(proposed, repair_windows),
                "changed_scalar_samples": changed,
                "removed_peak_absolute_sample": int(
                    np.max(np.abs(removed.astype(np.int64)))
                ),
                "removed_clipped_scalar_samples": removed_clipped,
            },
            "proof": {
                "source_unchanged": True,
                "immutable_source_snapshot": True,
                "lossless_preview_round_trip": True,
                "outside_approved_windows_and_channels_identical": True,
                "frame_count_equal": True,
                "format_equal": True,
                "removed_signal_matches_declared_difference": True,
            },
            "approval": {
                "status": "pending",
                "instruction": (
                    "Audition before.flac and proposed.flac at their unchanged original level, "
                    "and removed.flac as the declared-gain residue. This preview is not "
                    "permission to apply these candidate windows to the full recording."
                ),
            },
        }
        _atomic_json(stage / "preview.json", manifest)
        inputs.assert_unchanged()
        if bundle_dir.exists():
            raise GrooveSerpentError(f"Preview bundle already exists: {bundle_dir}")
        os.rename(stage, bundle_dir)
        committed = True
        result = dict(manifest)
        result["bundle_path"] = str(bundle_dir)
        return result
    finally:
        if not committed and stage.exists():
            if stage.parent != bundle_dir.parent or not stage.name.startswith(
                f".{bundle_dir.name}."
            ):
                raise GrooveSerpentError("Refusing unsafe preview staging cleanup.")
            shutil.rmtree(stage)


def _canonical_patch_bytes(values: np.ndarray, spec: _PcmSpec) -> bytes:
    return np.ascontiguousarray(values, dtype=spec.dtype).tobytes()


def _prepare_repair_patch(
    candidate: Mapping[str, Any],
    *,
    source_path: Path,
    source: Any,
    spec: _PcmSpec,
) -> dict[str, Any]:
    start = candidate["start_frame"]
    end = candidate["end_frame_exclusive"]
    peak = candidate["peak_frame"]
    channels = tuple(candidate["channels"])
    context_start = max(0, start - 512)
    context_end = min(source.sample_count, end + 512)
    before = _decode_array(
        source_path,
        sample_rate=source.sample_rate,
        channels=source.channels,
        spec=spec,
        start_frame=context_start,
        end_frame=context_end,
    )
    local_start = start - context_start
    local_end = end - context_start
    interval = ClickInterval(
        start_sample=local_start,
        end_sample=local_end,
        peak_sample=peak - context_start,
        confidence=float(candidate["confidence"]),
        channels=channels,
    )
    repaired = repair_click_intervals(before, [interval])
    source_values = before[local_start:local_end, list(channels)].copy()
    replacement = repaired[local_start:local_end, list(channels)].copy()
    if spec.bits == 24:
        replacement = _quantize_24_patch(replacement)
    replacement = np.ascontiguousarray(replacement, dtype=spec.dtype)
    changed = int(np.count_nonzero(source_values != replacement))
    if changed == 0:
        raise GrooveSerpentError(
            f"Approved candidate {candidate['id']} produced no PCM change."
        )
    return {
        "candidate_id": candidate["id"],
        "start_frame": start,
        "end_frame_exclusive": end,
        "channels": channels,
        "source_values": source_values,
        "replacement": replacement,
        "source_pcm_sha256": hashlib.sha256(
            _canonical_patch_bytes(source_values, spec)
        ).hexdigest(),
        "restored_pcm_sha256": hashlib.sha256(
            _canonical_patch_bytes(replacement, spec)
        ).hexdigest(),
        "changed_scalar_samples": changed,
    }


def _apply_patches_to_chunk(
    chunk: np.ndarray,
    *,
    absolute_start: int,
    patches: Sequence[Mapping[str, Any]],
) -> np.ndarray:
    absolute_end = absolute_start + chunk.shape[0]
    output = chunk
    copied = False
    for patch in patches:
        overlap_start = max(absolute_start, int(patch["start_frame"]))
        overlap_end = min(absolute_end, int(patch["end_frame_exclusive"]))
        if overlap_end <= overlap_start:
            continue
        if not copied:
            output = chunk.copy()
            copied = True
        chunk_start = overlap_start - absolute_start
        patch_start = overlap_start - int(patch["start_frame"])
        length = overlap_end - overlap_start
        for patch_channel, channel in enumerate(patch["channels"]):
            output[chunk_start : chunk_start + length, channel] = patch[
                "replacement"
            ][patch_start : patch_start + length, patch_channel]
    return output


def _encode_streamed_restored_flac(
    path: Path,
    *,
    source_path: Path,
    source: Any,
    spec: _PcmSpec,
    start_frame: int,
    end_frame: int,
    patches: Sequence[Mapping[str, Any]],
) -> None:
    ffmpeg = find_tool("ffmpeg")
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
        "-f",
        spec.format,
        "-ar",
        str(source.sample_rate),
        "-ac",
        str(source.channels),
        "-i",
        "pipe:0",
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
        "-c:a",
        "flac",
        "-compression_level",
        "8",
        "-sample_fmt",
        spec.sample_fmt,
        str(path),
    ]
    process: subprocess.Popen[bytes] | None = None
    stderr_thread: threading.Thread | None = None
    diagnostic_capture: BoundedDiagnostic | None = None
    source_iterator: Generator[np.ndarray, None, None] | None = None
    completed = False
    cursor = start_frame
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if process.stdin is None or process.stderr is None:
            raise GrooveSerpentError("FFmpeg did not expose the restoration encoder pipe.")
        diagnostic_capture, stderr_thread = start_diagnostic_reader(
            process,
            name="groove-serpent-restoration-stderr",
        )
        source_iterator = _decode_chunks(
            source_path,
            sample_rate=source.sample_rate,
            channels=source.channels,
            spec=spec,
            start_frame=start_frame,
            end_frame=end_frame,
        )
        for chunk in source_iterator:
            output = _apply_patches_to_chunk(
                chunk,
                absolute_start=cursor,
                patches=patches,
            )
            process.stdin.write(_canonical_patch_bytes(output, spec))
            cursor += chunk.shape[0]
        process.stdin.close()
        return_code = process.wait()
        join_diagnostic_reader(process, stderr_thread)
        diagnostic = diagnostic_capture.text() if diagnostic_capture else ""
        if return_code != 0:
            raise GrooveSerpentError(
                "FFmpeg could not encode the restored side"
                + (f": {diagnostic}" if diagnostic else ".")
            )
        if cursor != end_frame:
            raise GrooveSerpentError(
                f"Restoration streamed {cursor - start_frame} frames; expected "
                f"{end_frame - start_frame}."
            )
        completed = True
    except OSError as exc:
        raise GrooveSerpentError(f"Restoration PCM streaming failed: {exc}") from exc
    finally:
        if source_iterator is not None:
            source_iterator.close()
        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if not completed:
            terminate_and_reap(process)
        join_diagnostic_reader(process, stderr_thread)


def _verify_streamed_render(
    restored_path: Path,
    *,
    source_path: Path,
    source: Any,
    spec: _PcmSpec,
    start_frame: int,
    end_frame: int,
    patches: Sequence[Mapping[str, Any]],
) -> tuple[str, str]:
    source_iterator = _decode_chunks(
        source_path,
        sample_rate=source.sample_rate,
        channels=source.channels,
        spec=spec,
        start_frame=start_frame,
        end_frame=end_frame,
    )
    restored_iterator = _decode_chunks(
        restored_path,
        sample_rate=source.sample_rate,
        channels=source.channels,
        spec=spec,
        start_frame=0,
        end_frame=end_frame - start_frame,
    )
    empty = np.empty((0, source.channels), dtype=spec.dtype)
    source_buffer = empty
    restored_buffer = empty
    source_done = False
    restored_done = False
    verified = 0
    source_digest = hashlib.sha256()
    restored_digest = hashlib.sha256()
    try:
        while True:
            if not source_buffer.size and not source_done:
                try:
                    source_buffer = next(source_iterator)
                except StopIteration:
                    source_done = True
            if not restored_buffer.size and not restored_done:
                try:
                    restored_buffer = next(restored_iterator)
                except StopIteration:
                    restored_done = True
            if (
                source_done
                and restored_done
                and not source_buffer.size
                and not restored_buffer.size
            ):
                break
            if (source_done and not source_buffer.size) or (
                restored_done and not restored_buffer.size
            ):
                raise GrooveSerpentError(
                    "Restored PCM length differs from the source range."
                )
            count = min(source_buffer.shape[0], restored_buffer.shape[0])
            source_part = source_buffer[:count]
            restored_part = restored_buffer[:count]
            expected = _apply_patches_to_chunk(
                source_part,
                absolute_start=start_frame + verified,
                patches=patches,
            )
            if not np.array_equal(restored_part, expected):
                raise GrooveSerpentError(
                    "Restored PCM differs outside an approved window or from its approved patch."
                )
            source_digest.update(_canonical_patch_bytes(source_part, spec))
            restored_digest.update(_canonical_patch_bytes(restored_part, spec))
            verified += count
            source_buffer = source_buffer[count:]
            restored_buffer = restored_buffer[count:]
        if verified != end_frame - start_frame:
            raise GrooveSerpentError(
                "Restored PCM verification ended at the wrong frame count."
            )
        return source_digest.hexdigest(), restored_digest.hexdigest()
    finally:
        source_iterator.close()
        restored_iterator.close()


def render_restored_side(
    project_path: Path | str,
    scan_path: Path | str,
    recipe_path: Path | str,
    bundle_dir: Path | str,
    *,
    source_snapshot: VerifiedAudioSnapshot | None = None,
) -> dict[str, Any]:
    """Render one exact music-range FLAC from explicitly approved bounded repairs."""

    project_path = Path(project_path).expanduser().resolve()
    scan_path = Path(scan_path).expanduser().resolve()
    recipe_path = Path(recipe_path).expanduser().resolve()
    bundle_dir = Path(bundle_dir).expanduser().resolve()
    if bundle_dir.exists():
        raise GrooveSerpentError(f"Restoration bundle already exists: {bundle_dir}")
    inputs = _prepare_restoration_inputs(
        project_path,
        workspace=bundle_dir.parent,
        source_snapshot=source_snapshot,
        scan_path=scan_path,
        recipe_path=recipe_path,
    )
    try:
        return _render_restored_side(inputs, bundle_dir)
    finally:
        inputs.close()


def _render_restored_side(
    inputs: _RestorationInputs,
    bundle_dir: Path,
) -> dict[str, Any]:
    project_path = inputs.project_path
    project = inputs.project
    source_path = inputs.source_path
    source = inputs.source
    spec = inputs.spec
    scan_path = inputs.scan_path
    scan = inputs.scan
    scan_snapshot = inputs.scan_snapshot
    recipe_path = inputs.recipe_path
    recipe = inputs.recipe
    recipe_snapshot = inputs.recipe_snapshot
    if (
        scan_path is None
        or scan is None
        or scan_snapshot is None
        or recipe_path is None
        or recipe is None
        or recipe_snapshot is None
    ):
        raise AssertionError("Restoration render requires snapshotted scan and recipe inputs.")
    initial_project_sha256 = inputs.project_snapshot.live_receipt.sha256
    initial_scan_sha256 = scan_snapshot.live_receipt.sha256
    initial_recipe_sha256 = recipe_snapshot.live_receipt.sha256
    candidates = _validated_scan_candidates(
        scan,
        project_sha256=initial_project_sha256,
        source=source,
    )
    decisions = _validate_recipe_payload(
        recipe,
        project_path=project_path,
        project_sha256=initial_project_sha256,
        source_path=source_path,
        source_sha256=source.sha256,
        scan_path=scan_path,
        scan_sha256=initial_scan_sha256,
        candidates=candidates,
        expected_coverage=_restoration_coverage(scan, project),
    )
    coverage = _restoration_coverage(scan, project)
    if coverage["restoration_status"] != "complete":
        raise GrooveSerpentError(
            "restored.flac requires a full, untruncated scan of the exact project "
            "music range; this recipe represents only partial or exploratory repairs."
        )
    decision_by_id = {item["candidate_id"]: item for item in decisions}
    approved = [
        candidates[candidate_id]
        for candidate_id, decision in decision_by_id.items()
        if decision["decision"] == "approved"
    ]
    if not approved:
        raise GrooveSerpentError(
            "A restoration render requires at least one explicitly approved candidate."
        )
    protected_candidates = [
        candidates[candidate_id]
        for candidate_id, decision in decision_by_id.items()
        if decision["decision"] == "protected"
    ]
    approved.sort(
        key=lambda item: (
            item["start_frame"],
            item["end_frame_exclusive"],
            item["channels"],
        )
    )
    for index, left in enumerate(approved):
        for right in approved[index + 1 :]:
            if right["start_frame"] > left["end_frame_exclusive"]:
                break
            if (
                max(left["start_frame"], right["start_frame"])
                <= min(left["end_frame_exclusive"], right["end_frame_exclusive"])
                and not set(left["channels"]).isdisjoint(right["channels"])
            ):
                raise GrooveSerpentError(
                    "Approved repair windows overlap or touch in the same channel."
                )
    for approved_candidate in approved:
        for protected_candidate in protected_candidates:
            if (
                max(
                    approved_candidate["start_frame"],
                    protected_candidate["start_frame"],
                )
                <= min(
                    approved_candidate["end_frame_exclusive"],
                    protected_candidate["end_frame_exclusive"],
                )
                and not set(approved_candidate["channels"]).isdisjoint(
                    protected_candidate["channels"]
                )
            ):
                raise GrooveSerpentError(
                    "An approved repair overlaps or touches a protected needle/handling event."
                )

    music_start = project.tracks[0].start_sample
    music_end = project.tracks[-1].end_sample
    if any(
        candidate["start_frame"] < music_start
        or candidate["end_frame_exclusive"] > music_end
        for candidate in approved
    ):
        raise GrooveSerpentError(
            "Every approved repair must lie inside the exact project music range."
        )
    ensure_free_space(
        bundle_dir.parent,
        _restoration_storage_required_bytes(
            source_size_bytes=source.size_bytes,
            music_frame_count=music_end - music_start,
            channels=source.channels,
            bits_per_sample=spec.bits,
        ),
        label="Restoration render",
    )
    patches = [
        _prepare_repair_patch(
            candidate,
            source_path=inputs.source_snapshot.path,
            source=source,
            spec=spec,
        )
        for candidate in approved
    ]

    protected_paths = {
        project_path,
        scan_path,
        recipe_path,
        source_path.resolve(),
    }
    if bundle_dir in protected_paths:
        raise GrooveSerpentError("The restoration bundle cannot replace an input file.")
    bundle_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            dir=bundle_dir.parent,
            prefix=f".{bundle_dir.name}.",
            suffix=".partial",
        )
    ).resolve()
    committed = False
    try:
        restored_path = stage / "restored.flac"
        _encode_streamed_restored_flac(
            restored_path,
            source_path=inputs.source_snapshot.path,
            source=source,
            spec=spec,
            start_frame=music_start,
            end_frame=music_end,
            patches=patches,
        )
        restored_probe = probe_audio(restored_path)
        expected_shape = (
            "flac",
            source.sample_rate,
            source.channels,
            source.bits_per_raw_sample,
            music_end - music_start,
        )
        observed_shape = (
            restored_probe.codec_name.casefold(),
            restored_probe.sample_rate,
            restored_probe.channels,
            restored_probe.bits_per_raw_sample,
            restored_probe.sample_count,
        )
        if observed_shape != expected_shape:
            raise GrooveSerpentError("Restored FLAC format or frame count changed.")
        source_pcm_sha256, restored_pcm_sha256 = _verify_streamed_render(
            restored_path,
            source_path=inputs.source_snapshot.path,
            source=source,
            spec=spec,
            start_frame=music_start,
            end_frame=music_end,
            patches=patches,
        )
        repair_receipts = [
            {
                "candidate_id": patch["candidate_id"],
                "start_frame": patch["start_frame"],
                "end_frame_exclusive": patch["end_frame_exclusive"],
                "channels": list(patch["channels"]),
                "source_pcm_sha256": patch["source_pcm_sha256"],
                "restored_pcm_sha256": patch["restored_pcm_sha256"],
                "changed_scalar_samples": patch["changed_scalar_samples"],
            }
            for patch in patches
        ]
        receipt = {
            "schema": RENDER_SCHEMA,
            "created_at": utc_now_iso(),
            "app_version": __version__,
            "project": {
                "path": project_path.name,
                "sha256": initial_project_sha256,
            },
            "source": {
                "path": source_path.name,
                "sha256": source.sha256,
            },
            "scan": {
                "path": scan_path.name,
                "sha256": initial_scan_sha256,
            },
            "recipe": {
                "path": recipe_path.name,
                "sha256": initial_recipe_sha256,
                "schema": RECIPE_SCHEMA,
            },
            "music_range": {
                "start_frame": music_start,
                "end_frame_exclusive": music_end,
                "sample_count": music_end - music_start,
            },
            "coverage": coverage,
            "backend": {
                "name": REPAIR_BACKEND,
                "maximum_repair_frames": MAX_REPAIR_SAMPLES,
                "streaming_source_decode": True,
                "audacity_used": False,
                "immutable_source_snapshot": True,
                "source_snapshot_sha256": inputs.source_snapshot.sha256,
            },
            "repairs": repair_receipts,
            "protected": [
                {
                    "candidate_id": item["candidate_id"],
                    "classification": item["classification"],
                }
                for item in decisions
                if item["decision"] == "protected"
            ],
            "files": {
                "restored": {
                    "path": restored_path.name,
                    "sha256": sha256_file(restored_path),
                    "sample_count": music_end - music_start,
                    "sample_rate": source.sample_rate,
                    "channels": source.channels,
                    "bits_per_raw_sample": source.bits_per_raw_sample,
                }
            },
            "pcm_proof": {
                "source_music_range_sha256": source_pcm_sha256,
                "restored_music_range_sha256": restored_pcm_sha256,
                "outside_approved_windows_and_channels_identical": True,
                "approved_patches_match_receipt_hashes": True,
            },
            "proof": {
                "source_unchanged": True,
                "immutable_source_snapshot": True,
                "project_unchanged": True,
                "scan_unchanged": True,
                "recipe_unchanged": True,
                "lossless_flac_round_trip": True,
                "frame_count_equal_to_project_music_range": True,
                "format_equal_to_source": True,
            },
        }
        _atomic_json(stage / "render.json", receipt)
        inputs.assert_unchanged()
        if bundle_dir.exists():
            raise GrooveSerpentError(
                "An input or output path changed before restoration could be committed."
            )
        os.rename(stage, bundle_dir)
        committed = True
        result = dict(receipt)
        result["bundle_path"] = str(bundle_dir)
        return result
    finally:
        if not committed and stage.exists():
            if stage.parent != bundle_dir.parent or not stage.name.startswith(
                f".{bundle_dir.name}."
            ):
                raise GrooveSerpentError("Refusing unsafe restoration staging cleanup.")
            shutil.rmtree(stage)


__all__ = [
    "PREVIEW_SCHEMA",
    "RECIPE_SCHEMA",
    "RENDER_SCHEMA",
    "SCAN_SCHEMA",
    "create_click_preview",
    "create_restoration_recipe",
    "render_restored_side",
    "scan_project_clicks",
]
