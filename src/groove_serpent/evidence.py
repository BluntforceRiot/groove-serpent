"""Exact-sample waveform and spectrogram evidence for boundary review.

The evidence window is deliberately descriptive rather than authoritative.  It
keeps the source-sample selection, waveform, spectrum, and transient morphology
bound to the same decoded PCM so the browser cannot quietly compare different
regions or time grids.
"""

from __future__ import annotations

import copy
import json
import math
import subprocess
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .audio_snapshot import VerifiedAudioSnapshot, verified_audio_snapshot
from .errors import GrooveSerpentError, ProjectValidationError
from .media import MAX_DIAGNOSTIC_BYTES, find_tool
from .models import AudioSource, Project, resolve_source_path
from .project_io import load_project
from .subprocess_policy import terminate_and_reap


EVIDENCE_SCHEMA = "groove-serpent.evidence-window/1"
MAX_EVIDENCE_SECONDS = 30.0
MAX_WAVEFORM_POINTS = 2_000
MAX_SPECTROGRAM_TIME_BINS = 480
MAX_SPECTROGRAM_FREQUENCY_BINS = 128
MAX_EVIDENCE_CACHE_ENTRIES = 8
MAX_EVIDENCE_CACHE_BYTES = 32 * 1024 * 1024
# Evidence is a local microscope, not a bulk decoder.  This ceiling keeps one
# valid high-rate, high-channel request from materializing multi-gigabyte PCM
# while retaining a full 30-second window for ordinary stereo captures.
MAX_EVIDENCE_DECODE_BYTES = 256 * 1024 * 1024


class EvidenceRequestSuperseded(GrooveSerpentError):
    """A newer evidence request replaced work that must no longer publish."""


@dataclass(frozen=True, slots=True)
class EvidenceCacheKey:
    """Every input that can affect a deterministic evidence payload."""

    source_sha256: str
    source_filename: str
    source_sample_rate: int
    source_channels: int
    source_sample_count: int | None
    start_sample: int
    end_sample: int
    focus_sample: int
    waveform_points: int
    spectrogram_time_bins: int
    spectrogram_frequency_bins: int
    analysis_schema: str = EVIDENCE_SCHEMA


class EvidenceCache:
    """Small thread-safe LRU whose values cannot be mutated by callers."""

    def __init__(
        self,
        *,
        maximum_entries: int = MAX_EVIDENCE_CACHE_ENTRIES,
        maximum_bytes: int = MAX_EVIDENCE_CACHE_BYTES,
    ) -> None:
        if maximum_entries < 1 or maximum_bytes < 1:
            raise ValueError("Evidence cache limits must be positive.")
        self.maximum_entries = maximum_entries
        self.maximum_bytes = maximum_bytes
        self._entries: OrderedDict[
            EvidenceCacheKey, tuple[dict[str, Any], int]
        ] = OrderedDict()
        self._size_bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def _payload_size(payload: dict[str, Any]) -> int:
        return len(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        )

    def get(self, key: EvidenceCacheKey) -> dict[str, Any] | None:
        with self._lock:
            stored = self._entries.pop(key, None)
            if stored is None:
                return None
            self._entries[key] = stored
            return copy.deepcopy(stored[0])

    def put(self, key: EvidenceCacheKey, payload: dict[str, Any]) -> None:
        stored_payload = copy.deepcopy(payload)
        size = self._payload_size(stored_payload)
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._size_bytes -= previous[1]
            if size > self.maximum_bytes:
                return
            self._entries[key] = (stored_payload, size)
            self._size_bytes += size
            while (
                len(self._entries) > self.maximum_entries
                or self._size_bytes > self.maximum_bytes
            ):
                _discarded_key, (_discarded_payload, discarded_size) = (
                    self._entries.popitem(last=False)
                )
                self._size_bytes -= discarded_size

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._size_bytes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def evidence_cache_key(
    source: AudioSource,
    *,
    start_sample: int,
    end_sample: int,
    focus_sample: int | None,
    waveform_points: int = 1_200,
    spectrogram_time_bins: int = 320,
    spectrogram_frequency_bins: int = 96,
) -> EvidenceCacheKey:
    """Return the exact deterministic cache key for one evidence request."""

    if focus_sample is None:
        focus_sample = start_sample + (end_sample - start_sample) // 2
    return EvidenceCacheKey(
        source_sha256=str(source.sha256 or "").strip().lower(),
        source_filename=source.filename,
        source_sample_rate=source.sample_rate,
        source_channels=source.channels,
        source_sample_count=source.sample_count,
        start_sample=start_sample,
        end_sample=end_sample,
        focus_sample=focus_sample,
        waveform_points=waveform_points,
        spectrogram_time_bins=spectrogram_time_bins,
        spectrogram_frequency_bins=spectrogram_frequency_bins,
    )


def _raise_if_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise EvidenceRequestSuperseded(
            "This evidence request was superseded by a newer selection."
        )


def _strict_integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise ProjectValidationError(
            f"{label} must be a JSON integer between {minimum} and {maximum}."
        )
    return value


def _evidence_decode_size(frame_count: int, channels: int) -> int:
    """Return bounded float-PCM bytes without unsafe derived multiplication."""

    if type(frame_count) is not int or frame_count < 1:
        raise ProjectValidationError(
            "Evidence frame count must be a positive integer."
        )
    if type(channels) is not int or channels < 1:
        raise ProjectValidationError(
            "Evidence channel count must be a positive integer."
        )
    frame_bytes = channels * 4
    if frame_count > MAX_EVIDENCE_DECODE_BYTES // frame_bytes:
        raise ProjectValidationError(
            "Evidence window decoded PCM would exceed the "
            f"{MAX_EVIDENCE_DECODE_BYTES}-byte local safety limit. "
            "Shorten the window before refreshing evidence."
        )
    return frame_count * frame_bytes


def _dbfs_rms(values: np.ndarray) -> float:
    if values.size == 0:
        return -180.0
    power = float(np.mean(np.square(values, dtype=np.float64)))
    return float(20.0 * math.log10(max(math.sqrt(power), 1e-9)))


def _decode_exact_float(
    source_path: Path,
    *,
    start_sample: int,
    end_sample: int,
    sample_rate: int,
    channels: int,
    cancelled: Callable[[], bool] | None = None,
) -> np.ndarray:
    expected_frames = end_sample - start_sample
    expected_bytes = _evidence_decode_size(expected_frames, channels)
    ffmpeg = find_tool("ffmpeg")
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
        (
            f"atrim=start_sample={start_sample}:end_sample={end_sample},"
            "asettb=expr=1/"
            f"{sample_rate},asetpts=N"
        ),
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        "pipe:1",
    ]
    process: subprocess.Popen[bytes] | None = None
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        if cancelled is None:
            completed = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
            )
            returncode = completed.returncode
        else:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
                while True:
                    _raise_if_cancelled(cancelled)
                    try:
                        returncode = process.wait(timeout=0.025)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                terminate_and_reap(process)
        stderr_file.seek(0)
        diagnostic = stderr_file.read(MAX_DIAGNOSTIC_BYTES + 1)
        stdout_file.seek(0)
        decoded = stdout_file.read(expected_bytes + 1)
    _raise_if_cancelled(cancelled)
    if returncode != 0:
        detail = diagnostic[:MAX_DIAGNOSTIC_BYTES].decode(
            "utf-8", errors="replace"
        ).strip()
        if len(diagnostic) > MAX_DIAGNOSTIC_BYTES:
            detail += " [diagnostic truncated]"
        raise GrooveSerpentError(
            f"FFmpeg could not decode the evidence window: {detail}"
        )
    frame_bytes = channels * 4
    if len(decoded) % frame_bytes:
        raise GrooveSerpentError("The evidence decoder returned a partial PCM frame.")
    decoded_frames = len(decoded) // frame_bytes
    if decoded_frames != expected_frames:
        raise GrooveSerpentError(
            "The evidence decoder did not preserve the exact requested sample bounds "
            f"({decoded_frames} decoded; {expected_frames} expected)."
        )
    return np.frombuffer(decoded, dtype="<f4").reshape(
        expected_frames, channels
    )


def _waveform(pcm: np.ndarray, maximum_points: int) -> dict[str, Any]:
    frame_count, channel_count = pcm.shape
    point_count = min(maximum_points, frame_count)
    edges = np.linspace(0, frame_count, point_count + 1, dtype=np.int64)
    channels: list[dict[str, list[float]]] = []
    for channel in range(channel_count):
        minimum: list[float] = []
        maximum: list[float] = []
        rms: list[float] = []
        values = pcm[:, channel].astype(np.float64, copy=False)
        for index in range(point_count):
            bucket = values[edges[index] : edges[index + 1]]
            minimum.append(float(np.min(bucket)))
            maximum.append(float(np.max(bucket)))
            rms.append(float(np.sqrt(np.mean(np.square(bucket)))))
        channels.append({"minimum": minimum, "maximum": maximum, "rms": rms})
    return {
        "point_count": point_count,
        "bucket_edges_in_window_samples": [int(value) for value in edges],
        "channels": channels,
    }


def _spectrogram(
    pcm: np.ndarray,
    *,
    sample_rate: int,
    maximum_time_bins: int,
    frequency_bins: int,
) -> dict[str, Any]:
    channels = pcm.astype(np.float64)
    original_count = channels.shape[0]
    fft_size = 4_096 if sample_rate >= 48_000 else 2_048
    fft_size = min(fft_size, max(256, 1 << max(0, original_count.bit_length() - 1)))
    if original_count < fft_size:
        channels = np.pad(channels, ((0, fft_size - original_count), (0, 0)))
    available = max(0, channels.shape[0] - fft_size)
    natural_frames = max(1, 1 + available // max(1, fft_size // 4))
    time_count = min(maximum_time_bins, natural_frames)
    if time_count == 1:
        starts = np.asarray([0], dtype=np.int64)
    else:
        starts = np.linspace(0, available, time_count, dtype=np.int64)

    nyquist = sample_rate / 2.0
    low_hz = min(20.0, max(1.0, nyquist / 16.0))
    band_edges = np.geomspace(low_hz, nyquist, frequency_bins + 1)
    fft_frequencies = np.fft.rfftfreq(fft_size, d=1.0 / sample_rate)
    band_indices: list[np.ndarray] = []
    band_centers: list[float] = []
    for index in range(frequency_bins):
        if index == frequency_bins - 1:
            selected = np.flatnonzero(
                (fft_frequencies >= band_edges[index])
                & (fft_frequencies <= band_edges[index + 1])
            )
        else:
            selected = np.flatnonzero(
                (fft_frequencies >= band_edges[index])
                & (fft_frequencies < band_edges[index + 1])
            )
        if selected.size == 0:
            selected = np.asarray(
                [int(np.argmin(np.abs(fft_frequencies - math.sqrt(
                    band_edges[index] * band_edges[index + 1]
                ))))],
                dtype=np.int64,
            )
        band_indices.append(selected)
        band_centers.append(
            float(math.sqrt(band_edges[index] * band_edges[index + 1]))
        )

    window = np.hanning(fft_size)
    scale = max(float(np.sum(window)) / 2.0, 1e-12)
    matrix = np.empty((frequency_bins, time_count), dtype=np.float64)
    for column, start in enumerate(starts):
        segment = channels[start : start + fft_size] * window[:, np.newaxis]
        spectrum = np.abs(np.fft.rfft(segment, axis=0)) / scale
        # Average channel power instead of downmixing first.  A lateral or
        # out-of-phase transient must remain visible rather than cancelling.
        power = np.mean(np.square(spectrum), axis=1)
        for row, indices in enumerate(band_indices):
            matrix[row, column] = float(np.mean(power[indices]))
    db = 10.0 * np.log10(np.maximum(matrix, 1e-18))
    db = np.clip(db, -120.0, 0.0)
    center_offsets = np.minimum(
        starts + fft_size // 2,
        max(0, original_count - 1),
    )
    return {
        "fft_size": fft_size,
        "window": "hann",
        "time_bin_center_offsets": [int(value) for value in center_offsets],
        "frequency_hz": band_centers,
        "minimum_dbfs": -120.0,
        "maximum_dbfs": 0.0,
        # Rows run from low to high frequency; the renderer decides screen direction.
        "dbfs": [[float(value) for value in row] for row in db],
    }


def _transient_morphology(
    pcm: np.ndarray,
    *,
    sample_rate: int,
    start_sample: int,
) -> list[dict[str, Any]]:
    channels = pcm.astype(np.float64)
    if channels.shape[0] < 3:
        return []
    derivative = np.max(
        np.abs(np.diff(channels, axis=0, prepend=channels[0:1])), axis=1
    )
    median = float(np.median(derivative))
    mad = float(np.median(np.abs(derivative - median)))
    threshold = max(0.02, median + 12.0 * 1.4826 * max(mad, 1e-12))
    candidates = np.flatnonzero(derivative >= threshold)
    if candidates.size == 0:
        return []
    ordered = candidates[np.argsort(derivative[candidates])[::-1]]
    suppression = max(1, round(sample_rate * 0.015))
    selected: list[int] = []
    for candidate in ordered:
        value = int(candidate)
        if all(abs(value - existing) > suppression for existing in selected):
            selected.append(value)
        if len(selected) >= 12:
            break
    selected.sort()

    events: list[dict[str, Any]] = []
    guard = max(1, round(sample_rate * 0.002))
    context = max(guard + 1, round(sample_rate * 0.050))
    for offset in selected:
        before = channels[max(0, offset - context) : max(0, offset - guard)]
        after = channels[
            min(channels.shape[0], offset + guard) :
            min(channels.shape[0], offset + context)
        ]
        before_db = _dbfs_rms(before)
        after_db = _dbfs_rms(after)
        morphology = "isolated_transient"
        confidence = 0.0
        if before_db <= -48.0 and after_db >= before_db + 9.0:
            morphology = "needle_drop_candidate"
            confidence = min(1.0, (after_db - before_db - 9.0) / 24.0 + 0.35)
        elif after_db <= -48.0 and before_db >= after_db + 9.0:
            morphology = "needle_pickup_candidate"
            confidence = min(1.0, (before_db - after_db - 9.0) / 24.0 + 0.35)
        events.append(
            {
                "sample": start_sample + offset,
                "offset_in_window": offset,
                "time_seconds": (start_sample + offset) / sample_rate,
                "derivative_peak": float(derivative[offset]),
                "before_rms_dbfs": before_db,
                "after_rms_dbfs": after_db,
                "morphology_hint": morphology,
                "hint_confidence": confidence,
                "protected_by_default": morphology.startswith("needle_"),
            }
        )
    return events


def _focus_metrics(
    pcm: np.ndarray,
    *,
    focus_offset: int,
    sample_rate: int,
    transients: list[dict[str, Any]],
) -> dict[str, Any]:
    channels = pcm.astype(np.float64)
    context = max(1, round(sample_rate * 0.35))
    guard = max(1, round(sample_rate * 0.005))
    left = channels[max(0, focus_offset - context) : max(0, focus_offset - guard)]
    right = channels[
        min(channels.shape[0], focus_offset + guard) :
        min(channels.shape[0], focus_offset + context)
    ]
    left_db = _dbfs_rms(left)
    right_db = _dbfs_rms(right)
    change = right_db - left_db
    spectral_size = max(left.shape[0], right.shape[0], 256)
    spectral_size = 1 << (spectral_size - 1).bit_length()
    spectral_size = min(spectral_size, 65_536)

    def spectral_power(values: np.ndarray) -> np.ndarray:
        if values.shape[0] < spectral_size:
            values = np.pad(values, ((0, spectral_size - values.shape[0]), (0, 0)))
        else:
            values = values[:spectral_size]
        window = np.hanning(spectral_size)[:, np.newaxis]
        transformed = np.fft.rfft(values * window, axis=0)
        return np.asarray(
            np.mean(np.square(np.abs(transformed)), axis=1), dtype=np.float64
        )

    left_power = spectral_power(left)
    right_power = spectral_power(right)
    frequencies = np.fft.rfftfreq(spectral_size, 1.0 / sample_rate)
    left_total = max(float(np.sum(left_power)), 1e-18)
    right_total = max(float(np.sum(right_power)), 1e-18)
    left_centroid = float(np.sum(frequencies * left_power) / left_total)
    right_centroid = float(np.sum(frequencies * right_power) / right_total)
    similarity_denominator = math.sqrt(
        float(np.dot(left_power, left_power)) * float(np.dot(right_power, right_power))
    )
    spectral_similarity = (
        float(np.dot(left_power, right_power) / similarity_denominator)
        if similarity_denominator > 1e-18
        else 0.0
    )
    nearby = [
        item
        for item in transients
        if abs(int(item["offset_in_window"]) - focus_offset)
        <= round(sample_rate * 0.030)
    ]
    observations: list[str] = []
    if change <= -12.0:
        observations.append("Energy falls sharply after the selected sample.")
    elif change >= 12.0:
        observations.append("Energy rises sharply after the selected sample.")
    else:
        observations.append("Energy remains broadly continuous across the selected sample.")
    if nearby:
        observations.append("An impulse-like event occurs within 30 ms of the selection.")
    else:
        observations.append("No dominant impulse-like event occurs within 30 ms of the selection.")
    if right_db <= -55.0:
        observations.append("The immediate post-selection region is near the local noise floor.")
    if spectral_similarity >= 0.8:
        observations.append("Spectral shape remains strongly similar across the selection.")
    elif spectral_similarity <= 0.3:
        observations.append("Spectral shape changes substantially across the selection.")
    return {
        "left_rms_dbfs": left_db,
        "right_rms_dbfs": right_db,
        "right_minus_left_db": change,
        "left_spectral_centroid_hz": left_centroid,
        "right_spectral_centroid_hz": right_centroid,
        "spectral_cosine_similarity": spectral_similarity,
        "transient_within_30_ms": bool(nearby),
        "observations": observations,
        "interpretation_policy": (
            "These are descriptive measurements, not permission to move a marker or "
            "repair audio. Audition remains required."
        ),
    }


def analyze_evidence_window(
    source_path: Path | str,
    source: AudioSource,
    *,
    start_sample: int,
    end_sample: int,
    focus_sample: int | None = None,
    waveform_points: int = 1_200,
    spectrogram_time_bins: int = 320,
    spectrogram_frequency_bins: int = 96,
    source_snapshot: VerifiedAudioSnapshot | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Analyze one source window from a single immutable verified snapshot."""

    source_path = Path(source_path).expanduser().resolve()
    if source_snapshot is not None:
        if source_snapshot.live_path != source_path:
            raise ProjectValidationError(
                "The evidence snapshot belongs to a different source path."
            )
        if not source.sha256 or source_snapshot.sha256.lower() != source.sha256.lower():
            raise ProjectValidationError(
                "The evidence snapshot does not match the analyzed source."
            )
        source_snapshot.assert_evidence_lease()
        _raise_if_cancelled(cancelled)
        payload = _analyze_evidence_snapshot(
            source_snapshot.path,
            source,
            start_sample=start_sample,
            end_sample=end_sample,
            focus_sample=focus_sample,
            waveform_points=waveform_points,
            spectrogram_time_bins=spectrogram_time_bins,
            spectrogram_frequency_bins=spectrogram_frequency_bins,
            cancelled=cancelled,
        )
        source_snapshot.assert_evidence_lease()
        _raise_if_cancelled(cancelled)
        return payload

    if not source.sha256:
        raise ProjectValidationError(
            "Evidence review requires an analyzed source SHA-256."
        )
    with verified_audio_snapshot(
        source_path,
        expected_sha256=source.sha256,
        expected_size_bytes=source.size_bytes,
        label="Source audio",
    ) as snapshot:
        return _analyze_evidence_snapshot(
            snapshot.path,
            source,
            start_sample=start_sample,
            end_sample=end_sample,
            focus_sample=focus_sample,
            waveform_points=waveform_points,
            spectrogram_time_bins=spectrogram_time_bins,
            spectrogram_frequency_bins=spectrogram_frequency_bins,
            cancelled=cancelled,
        )


def _analyze_evidence_snapshot(
    source_path: Path,
    source: AudioSource,
    *,
    start_sample: int,
    end_sample: int,
    focus_sample: int | None,
    waveform_points: int,
    spectrogram_time_bins: int,
    spectrogram_frequency_bins: int,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Build synchronized evidence from an already verified snapshot path."""

    if source.sample_count is None:
        raise ProjectValidationError("Evidence review requires an exact source sample count.")
    start_sample = _strict_integer(
        start_sample, "Evidence start sample", 0, source.sample_count - 1
    )
    end_sample = _strict_integer(
        end_sample, "Evidence end sample", start_sample + 1, source.sample_count
    )
    maximum_frames = max(1, round(source.sample_rate * MAX_EVIDENCE_SECONDS))
    if end_sample - start_sample > maximum_frames:
        raise ProjectValidationError(
            f"Evidence windows cannot exceed {MAX_EVIDENCE_SECONDS:g} seconds."
        )
    _evidence_decode_size(end_sample - start_sample, source.channels)
    if focus_sample is None:
        focus_sample = start_sample + (end_sample - start_sample) // 2
    focus_sample = _strict_integer(
        focus_sample, "Evidence focus sample", start_sample, end_sample - 1
    )
    waveform_points = _strict_integer(
        waveform_points, "Waveform point count", 16, MAX_WAVEFORM_POINTS
    )
    spectrogram_time_bins = _strict_integer(
        spectrogram_time_bins,
        "Spectrogram time-bin count",
        8,
        MAX_SPECTROGRAM_TIME_BINS,
    )
    spectrogram_frequency_bins = _strict_integer(
        spectrogram_frequency_bins,
        "Spectrogram frequency-bin count",
        16,
        MAX_SPECTROGRAM_FREQUENCY_BINS,
    )

    _raise_if_cancelled(cancelled)
    decode_arguments: dict[str, Any] = {
        "start_sample": start_sample,
        "end_sample": end_sample,
        "sample_rate": source.sample_rate,
        "channels": source.channels,
    }
    if cancelled is not None:
        decode_arguments["cancelled"] = cancelled
    pcm = _decode_exact_float(source_path, **decode_arguments)
    _raise_if_cancelled(cancelled)
    transients = _transient_morphology(
        pcm, sample_rate=source.sample_rate, start_sample=start_sample
    )
    _raise_if_cancelled(cancelled)
    waveform = _waveform(pcm, waveform_points)
    _raise_if_cancelled(cancelled)
    spectrogram = _spectrogram(
        pcm,
        sample_rate=source.sample_rate,
        maximum_time_bins=spectrogram_time_bins,
        frequency_bins=spectrogram_frequency_bins,
    )
    _raise_if_cancelled(cancelled)
    focus_evidence = _focus_metrics(
        pcm,
        focus_offset=focus_sample - start_sample,
        sample_rate=source.sample_rate,
        transients=transients,
    )
    _raise_if_cancelled(cancelled)
    payload = {
        "schema": EVIDENCE_SCHEMA,
        "source": {
            "filename": source.filename,
            "sha256": source.sha256,
            "sample_rate": source.sample_rate,
            "channels": source.channels,
        },
        "selection": {
            "start_sample": start_sample,
            "end_sample_exclusive": end_sample,
            "focus_sample": focus_sample,
            "sample_count": end_sample - start_sample,
            "start_seconds": start_sample / source.sample_rate,
            "end_seconds": end_sample / source.sample_rate,
            "focus_seconds": focus_sample / source.sample_rate,
        },
        "waveform": waveform,
        "spectrogram": spectrogram,
        "transients": transients,
        "focus_evidence": focus_evidence,
    }
    return payload


def project_evidence_window(
    project_path: Path | str,
    *,
    start_sample: int,
    end_sample: int,
    focus_sample: int | None = None,
) -> dict[str, Any]:
    """Load a project, verify its immutable source, and build evidence."""

    project_path = Path(project_path).expanduser().resolve()
    project: Project = load_project(project_path)
    source_path = resolve_source_path(project, project_path).resolve()
    return analyze_evidence_window(
        source_path,
        project.source,
        start_sample=start_sample,
        end_sample=end_sample,
        focus_sample=focus_sample,
    )


__all__ = [
    "EVIDENCE_SCHEMA",
    "EvidenceCache",
    "EvidenceCacheKey",
    "EvidenceRequestSuperseded",
    "MAX_EVIDENCE_DECODE_BYTES",
    "MAX_EVIDENCE_SECONDS",
    "analyze_evidence_window",
    "evidence_cache_key",
    "project_evidence_window",
]
