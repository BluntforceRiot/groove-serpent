from __future__ import annotations

import hashlib
import json
import math
import subprocess
from pathlib import Path
from typing import Iterable

import numpy as np

from .errors import DependencyError, GrooveSerpentError
from .executable_discovery import find_executable
from .models import AudioSource
from .subprocess_policy import (
    BoundedDiagnostic,
    MEDIA_ANALYSIS_TIMEOUT_SECONDS,
    MEDIA_RENDER_TIMEOUT_SECONDS,
    join_diagnostic_reader,
    require_ffmpeg_nostdin,
    run_bounded_capture,
    start_diagnostic_reader,
    start_media_process,
    terminate_and_reap,
)


MAX_DIAGNOSTIC_BYTES = 64 * 1024


def audio_source_descriptor_mismatches(
    expected: AudioSource, actual: AudioSource
) -> tuple[str, ...]:
    """Compare saved audio authority with an independently probed byte snapshot.

    Paths and modification times intentionally differ for verified copies. Unknown
    optional stream fields are not wildcards: deleting a saved precision or frame
    count must not authorize a different decode. Duration keeps the legacy probe
    rounding tolerance; exact frame counts are compared separately.
    """

    fields = (
        "size_bytes", "sample_rate", "channels", "codec_name",
        "bits_per_raw_sample", "sample_format", "sample_count",
    )
    mismatches = [
        name for name in fields if getattr(expected, name) != getattr(actual, name)
    ]
    if not expected.sha256 or expected.sha256.casefold() != actual.sha256.casefold():
        mismatches.append("sha256")
    if (
        not math.isfinite(expected.duration_seconds)
        or not math.isfinite(actual.duration_seconds)
        or abs(expected.duration_seconds - actual.duration_seconds) > 0.05
    ):
        mismatches.append("duration_seconds")
    return tuple(mismatches)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_tool(name: str) -> str:
    path = find_executable(name)
    if path is None:
        raise DependencyError(
            f"Required executable '{name}' was not found on PATH. "
            "Install FFmpeg and make sure both ffmpeg and ffprobe are available."
        )
    return path


def tool_version(name: str) -> str:
    executable = find_tool(name)
    command = [executable, "-version"]
    if Path(executable).name.casefold().startswith("ffmpeg"):
        command = require_ffmpeg_nostdin(command)
    try:
        completed = run_bounded_capture(command)
    except subprocess.TimeoutExpired:
        return f"{name}: version unavailable"
    if (
        completed.returncode != 0
        or completed.stdout_truncated
        or completed.stderr_truncated
    ):
        return f"{name}: version unavailable"
    output = (completed.stdout or completed.stderr).decode(
        "utf-8", errors="replace"
    )
    first_line = output.splitlines()
    return first_line[0] if first_line else f"{name}: version unavailable"


def probe_audio(path: Path, stored_path: str | None = None) -> AudioSource:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise GrooveSerpentError(f"Input audio does not exist: {path}")

    ffprobe = find_tool("ffprobe")
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        (
            "stream=codec_name,sample_rate,channels,bits_per_raw_sample,"
            "bits_per_sample,sample_fmt,duration,duration_ts,time_base"
        ),
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = run_bounded_capture(command)
    except subprocess.TimeoutExpired as exc:
        raise GrooveSerpentError(
            f"ffprobe exceeded its safety deadline while reading '{path.name}'."
        ) from exc
    diagnostic = completed.stderr.decode("utf-8", errors="replace")
    if (
        completed.returncode != 0
        or completed.stdout_truncated
        or completed.stderr_truncated
    ):
        raise GrooveSerpentError(
            f"ffprobe could not read '{path.name}':\n"
            f"{diagnostic[:MAX_DIAGNOSTIC_BYTES].strip()}"
        )

    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
        stream = payload["streams"][0]
    except (KeyError, IndexError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GrooveSerpentError(f"No readable audio stream was found in {path}.") from exc

    duration_value = stream.get("duration") or payload.get("format", {}).get("duration")
    try:
        duration_seconds = float(duration_value)
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
    except (TypeError, ValueError, KeyError) as exc:
        raise GrooveSerpentError(f"Incomplete audio metadata for {path}.") from exc

    bit_value = stream.get("bits_per_raw_sample") or stream.get("bits_per_sample")
    try:
        bits = int(bit_value) if bit_value not in (None, "", "0", 0) else None
    except (TypeError, ValueError):
        bits = None

    sample_count: int | None = None
    try:
        duration_ts = int(stream["duration_ts"])
        numerator_text, denominator_text = str(stream["time_base"]).split("/", 1)
        numerator = int(numerator_text)
        denominator = int(denominator_text)
        if duration_ts > 0 and numerator > 0 and denominator > 0:
            sample_count = round(duration_ts * numerator * sample_rate / denominator)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        sample_count = None

    stat = path.stat()
    return AudioSource(
        path=stored_path or str(path),
        filename=path.name,
        size_bytes=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        duration_seconds=duration_seconds,
        sample_rate=sample_rate,
        channels=channels,
        codec_name=str(stream.get("codec_name", "unknown")),
        bits_per_raw_sample=bits,
        sample_format=stream.get("sample_fmt"),
        sample_count=sample_count,
        sha256=sha256_file(path),
    )


def decode_rms_envelope(
    path: Path,
    *,
    analysis_rate: int,
    window_ms: int,
    maximum_duration_seconds: float,
) -> tuple[list[float], float]:
    """Decode audio through FFmpeg and return one mono RMS dBFS value per window.

    The full-rate source is never loaded into memory. FFmpeg downmixes and
    downsamples it, while NumPy reduces streaming blocks into a compact envelope.
    """

    if analysis_rate <= 0 or window_ms <= 0:
        raise ValueError("analysis_rate and window_ms must be positive")
    if (
        not math.isfinite(maximum_duration_seconds)
        or maximum_duration_seconds <= 0
    ):
        raise ValueError("maximum_duration_seconds must be finite and positive")

    ffmpeg = find_tool("ffmpeg")
    window_samples = max(1, round(analysis_rate * window_ms / 1000.0))
    window_seconds = window_samples / analysis_rate
    maximum_samples = (
        math.ceil(maximum_duration_seconds * analysis_rate) + analysis_rate
    )
    maximum_output_bytes = maximum_samples * 4
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        str(analysis_rate),
        "-f",
        "f32le",
        "-fs",
        str(maximum_output_bytes),
        "pipe:1",
    ]

    process: subprocess.Popen[bytes] | None = None
    diagnostic_capture: BoundedDiagnostic | None = None
    diagnostic_thread = None
    completed = False
    try:
        process = start_media_process(
            require_ffmpeg_nostdin(command),
            timeout=MEDIA_ANALYSIS_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            raise GrooveSerpentError("FFmpeg did not expose the analysis pipes.")
        diagnostic_capture, diagnostic_thread = start_diagnostic_reader(
            process,
            name="groove-serpent-analysis-stderr",
        )

        values: list[float] = []
        sample_remainder = np.empty(0, dtype="<f4")
        byte_remainder = b""
        decoded_samples = 0

        while True:
            chunk = process.stdout.read(4 * 1024 * 1024)
            if not chunk:
                break
            chunk = byte_remainder + chunk
            usable_bytes = len(chunk) - (len(chunk) % 4)
            byte_remainder = chunk[usable_bytes:]
            if usable_bytes == 0:
                continue
            decoded_samples += usable_bytes // 4
            if decoded_samples >= maximum_samples:
                raise GrooveSerpentError(
                    "FFmpeg analysis exceeded the validated PCM output bound."
                )
            samples = np.frombuffer(chunk[:usable_bytes], dtype="<f4")
            if sample_remainder.size:
                samples = np.concatenate((sample_remainder, samples))

            complete_count = (samples.size // window_samples) * window_samples
            if complete_count:
                blocks = samples[:complete_count].reshape(-1, window_samples)
                power = np.mean(np.square(blocks, dtype=np.float64), axis=1)
                rms = np.sqrt(power)
                db = 20.0 * np.log10(np.maximum(rms, 1e-9))
                values.extend(float(item) for item in db)
            sample_remainder = samples[complete_count:].copy()

        if sample_remainder.size:
            power = float(np.mean(np.square(sample_remainder, dtype=np.float64)))
            values.append(20.0 * math.log10(max(math.sqrt(power), 1e-9)))

        process.stdout.close()
        return_code = process.wait()
        join_diagnostic_reader(process, diagnostic_thread)
        stderr = diagnostic_capture.text() if diagnostic_capture else ""
        completed = True
    except subprocess.TimeoutExpired as exc:
        raise GrooveSerpentError(
            f"FFmpeg analysis exceeded its safety deadline for '{path.name}'."
        ) from exc
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
        raise GrooveSerpentError(
            f"FFmpeg failed while analyzing '{path.name}':\n{stderr.strip()}"
        )
    if not values:
        raise GrooveSerpentError(f"No audio samples were decoded from '{path.name}'.")
    return values, window_seconds


def run_ffmpeg(command: Iterable[str]) -> None:
    """Run an internally constructed, error-only FFmpeg command.

    Every caller uses ``-loglevel error``. Some FFmpeg builds report a refused
    no-overwrite output on stderr but exit zero, so a zero exit alone cannot
    authorize verification of whatever file happens to occupy that path.
    """
    process: subprocess.Popen[bytes] | None = None
    diagnostic_capture: BoundedDiagnostic | None = None
    diagnostic_thread = None
    completed = False
    try:
        process = start_media_process(
            require_ffmpeg_nostdin(list(command)),
            timeout=MEDIA_RENDER_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        diagnostic_capture, diagnostic_thread = start_diagnostic_reader(
            process,
            name="groove-serpent-ffmpeg-stderr",
        )
        return_code = process.wait()
        join_diagnostic_reader(process, diagnostic_thread)
        diagnostic = diagnostic_capture.text()
        completed = True
    except subprocess.TimeoutExpired as exc:
        raise GrooveSerpentError("FFmpeg exceeded the media render safety deadline.") from exc
    finally:
        if not completed:
            terminate_and_reap(process)
        join_diagnostic_reader(process, diagnostic_thread)
    if return_code != 0 or diagnostic:
        raise GrooveSerpentError(
            diagnostic or "FFmpeg failed without an error message."
        )
