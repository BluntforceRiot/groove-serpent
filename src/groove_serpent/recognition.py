from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Literal, Protocol, TypedDict, cast, runtime_checkable

from . import __user_agent__
from .album_publication_policy import speed_correction_details
from .audio_snapshot import VerifiedAudioSnapshot, verified_audio_snapshot
from .errors import GrooveSerpentError, ProjectValidationError
from .executable_discovery import find_executable
from .subprocess_policy import (
    BoundedDiagnostic,
    MAX_DIAGNOSTIC_BYTES,
    join_diagnostic_reader,
    run_bounded_capture,
    start_diagnostic_reader,
    terminate_and_reap,
)

_MAX_DIAGNOSTIC_BYTES = MAX_DIAGNOSTIC_BYTES


ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
_MAX_FINGERPRINT_SECONDS = 120.0
_LEAD_AMBIENCE_SECONDS = 8.0
_MIN_AUDIO_AFTER_SKIP_SECONDS = 15.0
_FP_SAMPLE_RATE = 11_025
_FINGERPRINT_CAPABILITY_TIMEOUT_SECONDS = 10.0
_MAX_FINGERPRINT_OUTPUT_BYTES = 64 * 1024
_MAX_FINGERPRINT_CHARACTERS = 16 * 1024
_FINGERPRINT_RE = re.compile(r"[A-Za-z0-9_-]+={0,2}\Z")
_REPARSE_POINT_ATTRIBUTE = 0x400
_ACOUSTID_RATE_LOCK = threading.Lock()
_acoustid_last_request_started = 0.0
RECOGNITION_SPEED_TRANSFORM = "integer-asetrate-pitch-and-tempo/1"


class _FingerprintPayload(TypedDict):
    fingerprint: str
    duration: int


@dataclass(frozen=True, slots=True)
class FingerprintBackendReadiness:
    """Bounded local fingerprint capability without network configuration."""

    ready: bool
    backend: str
    message: str
    ffmpeg: str = ""
    fpcalc: str = ""
    direct_capability: str = "unavailable"
    missing: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "backend": self.backend,
            "message": self.message,
            "ffmpeg": self.ffmpeg,
            "fpcalc": self.fpcalc,
            "direct_capability": self.direct_capability,
            "missing": list(self.missing),
        }


@dataclass(frozen=True, slots=True)
class _ExecutableIdentity:
    path: str
    size: int
    modified_ns: int
    changed_ns: int
    device: int
    inode: int
    mode: int
    file_attributes: int


@dataclass(frozen=True, slots=True)
class _FingerprintRuntime:
    backend: Literal["ffmpeg-chromaprint", "fpcalc"]
    ffmpeg: _ExecutableIdentity
    fpcalc: _ExecutableIdentity | None = None


class _BoundedBinaryCapture:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._payload = bytearray()
        self.truncated = False

    def drain(self, stream: BinaryIO) -> None:
        try:
            while True:
                chunk = stream.read(8_192)
                if not chunk:
                    break
                room = self._limit - len(self._payload)
                if room > 0:
                    self._payload.extend(chunk[:room])
                if len(chunk) > room:
                    self.truncated = True
        except (OSError, ValueError):
            self.truncated = True
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def payload(self) -> bytes:
        return bytes(self._payload)


class RecognitionError(GrooveSerpentError):
    """A user-facing failure from an optional recognition provider."""


@dataclass(frozen=True, slots=True)
class RecognitionReadiness:
    """Describes whether a recognition provider can be used right now."""

    provider: str
    enabled: bool
    ready: bool
    message: str
    missing: tuple[str, ...] = ()
    fingerprint_backend: str = ""

    @property
    def available(self) -> bool:
        """Compatibility-friendly synonym for ``ready``."""

        return self.ready

    @property
    def reason(self) -> str:
        """Compatibility-friendly synonym for the human-readable message."""

        return self.message

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "enabled": self.enabled,
            "ready": self.ready,
            "message": self.message,
            "missing": list(self.missing),
            "fingerprint_backend": self.fingerprint_backend,
        }


@dataclass(frozen=True, slots=True)
class RecognitionMatch:
    """One normalized recording match returned by a recognition provider."""

    title: str
    artist_credit: str
    score: float
    recording_mbid: str | None = None
    release_candidates: tuple[dict[str, object], ...] = field(default_factory=tuple)
    release_group_ids: tuple[str, ...] = field(default_factory=tuple)
    provider: str = "acoustid"

    @property
    def artist(self) -> str:
        """Short alias useful to callers that do not model artist credits."""

        return self.artist_credit

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "title": self.title,
            "artist_credit": self.artist_credit,
            "score": self.score,
            "recording_mbid": self.recording_mbid,
            "release_candidates": [dict(item) for item in self.release_candidates],
            "release_group_ids": list(self.release_group_ids),
        }


@runtime_checkable
class RecognitionProvider(Protocol):
    """Replaceable interface for optional audio-recognition backends."""

    name: str

    def readiness(self) -> RecognitionReadiness:
        """Return configuration/runtime status without raising an exception."""

    def identify_track(
        self,
        source_path: str | Path | VerifiedAudioSnapshot,
        start_sample: int,
        end_sample: int,
        sample_rate: int,
        *,
        source_speed_factor: float = 1.0,
    ) -> list[RecognitionMatch]:
        """Identify an exact source range at its reviewed playback speed."""


class NoRecognitionProvider:
    """Provider used when online recognition has not been opted into."""

    name = "none"

    def readiness(self) -> RecognitionReadiness:
        return RecognitionReadiness(
            provider=self.name,
            enabled=False,
            ready=False,
            message="Audio recognition is disabled; splitting remains fully available.",
        )

    def identify_track(
        self,
        source_path: str | Path | VerifiedAudioSnapshot,
        start_sample: int,
        end_sample: int,
        sample_rate: int,
        *,
        source_speed_factor: float = 1.0,
    ) -> list[RecognitionMatch]:
        del source_path, start_sample, end_sample, sample_rate, source_speed_factor
        return []


def _capture_executable_identity(value: str, label: str) -> _ExecutableIdentity:
    try:
        path = Path(value).expanduser().resolve(strict=True)
        observed = path.lstat()
    except OSError as exc:
        raise RecognitionError(f"{label} could not be inspected.") from exc
    attributes = int(getattr(observed, "st_file_attributes", 0))
    if (
        stat.S_ISLNK(observed.st_mode)
        or bool(attributes & _REPARSE_POINT_ATTRIBUTE)
        or not stat.S_ISREG(observed.st_mode)
    ):
        raise RecognitionError(f"{label} is not a regular executable file.")
    return _ExecutableIdentity(
        path=str(path),
        size=int(observed.st_size),
        modified_ns=int(observed.st_mtime_ns),
        changed_ns=int(observed.st_ctime_ns),
        device=int(observed.st_dev),
        inode=int(observed.st_ino),
        mode=int(observed.st_mode),
        file_attributes=attributes,
    )


def _assert_executable_unchanged(
    expected: _ExecutableIdentity,
    label: str,
) -> None:
    current = _capture_executable_identity(expected.path, label)
    if current != expected:
        raise RecognitionError(f"{label} changed during fingerprinting.")


def _probe_ffmpeg_chromaprint(
    ffmpeg: _ExecutableIdentity,
) -> tuple[Literal["ready", "absent", "malformed", "timeout"], str]:
    command = [
        ffmpeg.path,
        "-nostdin",
        "-hide_banner",
        "-h",
        "muxer=chromaprint",
    ]
    try:
        completed = run_bounded_capture(
            command,
            stdout_limit=_MAX_DIAGNOSTIC_BYTES,
            stderr_limit=_MAX_DIAGNOSTIC_BYTES,
            timeout=_FINGERPRINT_CAPABILITY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "timeout", "FFmpeg Chromaprint capability detection timed out."
    except (OSError, RuntimeError, ValueError) as exc:
        return "malformed", f"FFmpeg Chromaprint capability detection failed: {exc}"
    try:
        _assert_executable_unchanged(ffmpeg, "FFmpeg executable")
    except RecognitionError as exc:
        return "malformed", str(exc)
    combined = completed.stdout + b"\n" + completed.stderr
    diagnostic = _diagnostic_text(combined)
    if completed.stdout_truncated or completed.stderr_truncated:
        return "malformed", "FFmpeg Chromaprint capability output exceeded its bound."
    if "unknown format 'chromaprint'" in diagnostic.casefold():
        return "absent", "FFmpeg does not provide the Chromaprint muxer."
    required = (
        "Muxer chromaprint [Chromaprint]:",
        "Default audio codec: pcm_s16le.",
        "-fp_format",
        "base64",
    )
    if completed.returncode == 0 and all(token in diagnostic for token in required):
        return "ready", "FFmpeg provides the bounded Chromaprint base64 muxer path."
    return "malformed", "FFmpeg returned malformed Chromaprint capability output."


def _discover_fingerprint_runtime(
) -> tuple[FingerprintBackendReadiness, _FingerprintRuntime | None]:
    try:
        ffmpeg_value = find_executable("ffmpeg")
    except (OSError, ValueError) as exc:
        try:
            fpcalc_value = _find_fpcalc()
        except (OSError, ValueError):
            fpcalc_value = None
        missing = ("ffmpeg",) if fpcalc_value else ("fpcalc", "ffmpeg")
        return (
            FingerprintBackendReadiness(
                ready=False,
                backend="",
                message=f"FFmpeg discovery failed: {exc}",
                fpcalc=fpcalc_value or "",
                missing=missing,
            ),
            None,
        )
    if ffmpeg_value is None:
        try:
            fpcalc_value = _find_fpcalc()
        except (OSError, ValueError):
            fpcalc_value = None
        missing = ("ffmpeg",) if fpcalc_value else ("fpcalc", "ffmpeg")
        return (
            FingerprintBackendReadiness(
                ready=False,
                backend="",
                message="Acoustic fingerprinting is unavailable because FFmpeg is missing.",
                fpcalc=fpcalc_value or "",
                missing=missing,
            ),
            None,
        )
    try:
        ffmpeg = _capture_executable_identity(ffmpeg_value, "FFmpeg executable")
    except RecognitionError as exc:
        return (
            FingerprintBackendReadiness(
                ready=False,
                backend="",
                message=str(exc),
                missing=("ffmpeg",),
            ),
            None,
        )
    capability, capability_message = _probe_ffmpeg_chromaprint(ffmpeg)
    if capability == "ready":
        status = FingerprintBackendReadiness(
            ready=True,
            backend="ffmpeg-chromaprint",
            message=(
                "Acoustic fingerprinting is ready through FFmpeg's Chromaprint muxer."
            ),
            ffmpeg=ffmpeg.path,
            direct_capability=capability,
        )
        return status, _FingerprintRuntime("ffmpeg-chromaprint", ffmpeg)
    if capability != "absent":
        return (
            FingerprintBackendReadiness(
                ready=False,
                backend="",
                message=capability_message,
                ffmpeg=ffmpeg.path,
                direct_capability=capability,
                missing=("ffmpeg-chromaprint-capability",),
            ),
            None,
        )
    try:
        fpcalc_value = _find_fpcalc()
    except (OSError, ValueError):
        fpcalc_value = None
    if fpcalc_value is None:
        return (
            FingerprintBackendReadiness(
                ready=False,
                backend="",
                message=(
                    "Acoustic fingerprinting is unavailable: FFmpeg lacks its "
                    "Chromaprint muxer and fpcalc was not found."
                ),
                ffmpeg=ffmpeg.path,
                direct_capability=capability,
                missing=("ffmpeg-chromaprint", "fpcalc"),
            ),
            None,
        )
    try:
        fpcalc = _capture_executable_identity(fpcalc_value, "fpcalc executable")
    except RecognitionError as exc:
        return (
            FingerprintBackendReadiness(
                ready=False,
                backend="",
                message=str(exc),
                ffmpeg=ffmpeg.path,
                direct_capability=capability,
                missing=("fpcalc",),
            ),
            None,
        )
    status = FingerprintBackendReadiness(
        ready=True,
        backend="fpcalc",
        message="Acoustic fingerprinting is ready through the fpcalc compatibility path.",
        ffmpeg=ffmpeg.path,
        fpcalc=fpcalc.path,
        direct_capability=capability,
    )
    return status, _FingerprintRuntime("fpcalc", ffmpeg, fpcalc)


def fingerprint_backend_readiness() -> FingerprintBackendReadiness:
    """Return local backend readiness without reading audio or using the network."""

    status, _runtime = _discover_fingerprint_runtime()
    return status


def _start_binary_capture(
    stream: BinaryIO,
    *,
    name: str,
) -> tuple[_BoundedBinaryCapture, threading.Thread]:
    capture = _BoundedBinaryCapture(_MAX_FINGERPRINT_OUTPUT_BYTES)
    thread = threading.Thread(
        target=capture.drain,
        args=(stream,),
        name=name,
        daemon=True,
    )
    thread.start()
    return capture, thread


def _join_binary_capture(
    process: subprocess.Popen[bytes] | None,
    thread: threading.Thread | None,
) -> None:
    if thread is None:
        return
    thread.join(timeout=2.0)
    if thread.is_alive() and process is not None and process.stdout is not None:
        try:
            process.stdout.close()
        except (OSError, ValueError):
            pass
        thread.join(timeout=2.0)


class AcoustIDRecognitionProvider:
    """Optional AcoustID provider with local FFmpeg/fpcalc fingerprinting."""

    name = "acoustid"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        enabled: bool | None = None,
        timeout_seconds: float = 20.0,
        fingerprint_timeout_seconds: float = 150.0,
        max_response_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        # Supplying a key, directly or through the dedicated environment variable,
        # is the opt-in. An explicit false value remains a useful privacy switch.
        raw_key = api_key if api_key is not None else os.environ.get(
            "GROOVE_SERPENT_ACOUSTID_KEY", ""
        )
        self._api_key = raw_key.strip()
        self._enabled = bool(self._api_key) if enabled is None else bool(enabled)
        if timeout_seconds <= 0 or fingerprint_timeout_seconds <= 0:
            raise ValueError("Recognition timeouts must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._timeout_seconds = float(timeout_seconds)
        self._fingerprint_timeout_seconds = float(fingerprint_timeout_seconds)
        self._max_response_bytes = int(max_response_bytes)

    def readiness(self) -> RecognitionReadiness:
        missing: list[str] = []
        if not self._enabled:
            if not self._api_key:
                missing.append("api_key")
                message = (
                    "AcoustID recognition is not enabled. Supply api_key or set "
                    "GROOVE_SERPENT_ACOUSTID_KEY to opt in."
                )
            else:
                message = "AcoustID recognition was explicitly disabled."
            return RecognitionReadiness(
                provider=self.name,
                enabled=False,
                ready=False,
                message=message,
                missing=tuple(missing),
            )

        if not self._api_key:
            missing.append("api_key")
        backend_status, _runtime = _discover_fingerprint_runtime()
        missing.extend(backend_status.missing)

        if missing:
            labels = {
                "api_key": "an AcoustID API key",
                "fpcalc": "the Chromaprint fpcalc executable",
                "ffmpeg": "FFmpeg",
                "ffmpeg-chromaprint": "FFmpeg's Chromaprint muxer",
                "ffmpeg-chromaprint-capability": (
                    "a trustworthy FFmpeg Chromaprint capability result"
                ),
            }
            readable = ", ".join(labels.get(item, item) for item in missing)
            return RecognitionReadiness(
                provider=self.name,
                enabled=True,
                ready=False,
                message=f"AcoustID recognition is unavailable: missing {readable}.",
                missing=tuple(missing),
                fingerprint_backend=backend_status.backend,
            )

        return RecognitionReadiness(
            provider=self.name,
            enabled=True,
            ready=True,
            message=(
                "AcoustID recognition is ready with the "
                f"{backend_status.backend} fingerprint backend."
            ),
            fingerprint_backend=backend_status.backend,
        )

    def identify_track(
        self,
        source_path: str | Path | VerifiedAudioSnapshot,
        start_sample: int,
        end_sample: int,
        sample_rate: int,
        *,
        source_speed_factor: float = 1.0,
    ) -> list[RecognitionMatch]:
        status = self.readiness()
        if not status.ready:
            raise RecognitionError(status.message)
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if start_sample < 0 or end_sample <= start_sample:
            raise ValueError("Track sample boundaries are invalid")

        if not isinstance(source_path, VerifiedAudioSnapshot):
            path = Path(source_path).expanduser().resolve()
            if not path.is_file():
                raise RecognitionError(
                    f"Recognition source audio does not exist: {path}"
                )
            with verified_audio_snapshot(
                path,
                label="Recognition source audio",
            ) as snapshot:
                return self.identify_track(
                    snapshot,
                    start_sample,
                    end_sample,
                    sample_rate,
                    source_speed_factor=source_speed_factor,
                )

        snapshot = source_path
        snapshot.assert_snapshot_unchanged()
        path = snapshot.path

        backend_status, runtime = _discover_fingerprint_runtime()
        # Readiness is deliberately non-raising, but a runtime can be removed or
        # replaced between that check and use. Re-discover and bind exact path
        # identities at the consequential boundary.
        if runtime is None:
            raise RecognitionError(
                "Recognition runtime became unavailable: " + backend_status.message
            )

        excerpt_start, excerpt_end = _excerpt_sample_bounds(
            start_sample,
            end_sample,
            sample_rate,
            source_speed_factor=source_speed_factor,
        )
        fingerprint_payload = self._fingerprint(
            path,
            excerpt_start,
            excerpt_end,
            sample_rate,
            runtime=runtime,
            source_speed_factor=source_speed_factor,
        )
        response = self._lookup(
            fingerprint=fingerprint_payload["fingerprint"],
            duration=fingerprint_payload["duration"],
        )
        matches = _parse_matches(response)
        snapshot.assert_snapshot_unchanged()
        snapshot.assert_live_unchanged()
        return matches

    def _fingerprint(
        self,
        source_path: Path,
        excerpt_start: int,
        excerpt_end: int,
        sample_rate: int,
        *,
        runtime: _FingerprintRuntime,
        source_speed_factor: float = 1.0,
    ) -> _FingerprintPayload:
        if runtime.backend == "ffmpeg-chromaprint":
            return self._fingerprint_with_ffmpeg(
                source_path,
                excerpt_start,
                excerpt_end,
                sample_rate,
                runtime=runtime,
                source_speed_factor=source_speed_factor,
            )
        return self._fingerprint_with_fpcalc(
            source_path,
            excerpt_start,
            excerpt_end,
            sample_rate,
            runtime=runtime,
            source_speed_factor=source_speed_factor,
        )

    def _fingerprint_with_ffmpeg(
        self,
        source_path: Path,
        excerpt_start: int,
        excerpt_end: int,
        sample_rate: int,
        *,
        runtime: _FingerprintRuntime,
        source_speed_factor: float = 1.0,
    ) -> _FingerprintPayload:
        asetrate_hz, _effective_factor = _recognition_speed_details(
            sample_rate,
            source_speed_factor,
        )
        authoritative_duration = _fingerprint_duration(
            excerpt_start,
            excerpt_end,
            sample_rate,
            source_speed_factor=source_speed_factor,
        )
        filter_parts = [
            f"atrim=start_sample={excerpt_start}:end_sample={excerpt_end}",
            "asetpts=PTS-STARTPTS",
        ]
        if asetrate_hz != sample_rate:
            filter_parts.append(f"asetrate={asetrate_hz}")
        filter_graph = ",".join(filter_parts)
        command = [
            runtime.ffmpeg.path,
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
            "-ac",
            "1",
            "-ar",
            str(_FP_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-algorithm",
            "1",
            "-fp_format",
            "base64",
            "-f",
            "chromaprint",
            "pipe:1",
        ]
        process: subprocess.Popen[bytes] | None = None
        diagnostic: BoundedDiagnostic | None = None
        stderr_thread: threading.Thread | None = None
        output_capture: _BoundedBinaryCapture | None = None
        output_thread: threading.Thread | None = None
        try:
            _assert_executable_unchanged(runtime.ffmpeg, "FFmpeg executable")
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if process.stdout is None or process.stderr is None:
                raise RecognitionError("FFmpeg did not expose its fingerprint pipes.")
            output_capture, output_thread = _start_binary_capture(
                cast(BinaryIO, process.stdout),
                name="groove-serpent-chromaprint-stdout",
            )
            diagnostic, stderr_thread = start_diagnostic_reader(
                process,
                name="groove-serpent-chromaprint-stderr",
            )
            try:
                returncode = process.wait(timeout=self._fingerprint_timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                raise RecognitionError(
                    "Audio fingerprinting timed out; FFmpeg was stopped."
                ) from exc
            _join_binary_capture(process, output_thread)
            join_diagnostic_reader(process, stderr_thread)
            if output_thread is not None and output_thread.is_alive():
                raise RecognitionError("FFmpeg fingerprint output did not close cleanly.")
            error = diagnostic.text() if diagnostic else ""
            if returncode != 0:
                raise RecognitionError(
                    "FFmpeg could not fingerprint the recognition excerpt"
                    + (f": {error}" if error else ".")
                )
            if output_capture is None or output_capture.truncated:
                raise RecognitionError("FFmpeg fingerprint output exceeded its bound.")
            return {
                "fingerprint": _parse_direct_fingerprint(output_capture.payload()),
                "duration": authoritative_duration,
            }
        except OSError as exc:
            raise RecognitionError(f"Could not start the recognition runtime: {exc}") from exc
        finally:
            terminate_and_reap(process)
            _join_binary_capture(process, output_thread)
            join_diagnostic_reader(process, stderr_thread)
            _assert_executable_unchanged(runtime.ffmpeg, "FFmpeg executable")

    def _fingerprint_with_fpcalc(
        self,
        source_path: Path,
        excerpt_start: int,
        excerpt_end: int,
        sample_rate: int,
        *,
        runtime: _FingerprintRuntime,
        source_speed_factor: float = 1.0,
    ) -> _FingerprintPayload:
        if runtime.fpcalc is None:
            raise RecognitionError("The fpcalc compatibility runtime is incomplete.")
        authoritative_duration = _fingerprint_duration(
            excerpt_start,
            excerpt_end,
            sample_rate,
            source_speed_factor=source_speed_factor,
        )
        length_seconds = authoritative_duration
        asetrate_hz, _effective_factor = _recognition_speed_details(
            sample_rate,
            source_speed_factor,
        )
        filter_parts = [
            f"atrim=start_sample={excerpt_start}:end_sample={excerpt_end}",
            "asetpts=PTS-STARTPTS",
        ]
        if asetrate_hz != sample_rate:
            filter_parts.append(f"asetrate={asetrate_hz}")
        filter_graph = ",".join(filter_parts)
        ffmpeg_command = [
            runtime.ffmpeg.path,
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
            "-ac",
            "1",
            "-ar",
            str(_FP_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            "pipe:1",
        ]
        fpcalc_command = [
            runtime.fpcalc.path,
            "-json",
            "-length",
            str(length_seconds),
            "-",
        ]

        ffmpeg_process: subprocess.Popen[bytes] | None = None
        fpcalc_process: subprocess.Popen[bytes] | None = None
        ffmpeg_diagnostic: BoundedDiagnostic | None = None
        ffmpeg_stderr_thread: threading.Thread | None = None
        fpcalc_diagnostic: BoundedDiagnostic | None = None
        fpcalc_stderr_thread: threading.Thread | None = None
        output_capture: _BoundedBinaryCapture | None = None
        output_thread: threading.Thread | None = None
        try:
            _assert_executable_unchanged(runtime.ffmpeg, "FFmpeg executable")
            _assert_executable_unchanged(runtime.fpcalc, "fpcalc executable")
            ffmpeg_process = subprocess.Popen(
                ffmpeg_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if ffmpeg_process.stdout is None or ffmpeg_process.stderr is None:
                raise RecognitionError("FFmpeg did not expose the required audio pipe.")

            ffmpeg_diagnostic, ffmpeg_stderr_thread = start_diagnostic_reader(
                ffmpeg_process,
                name="groove-serpent-ffmpeg-stderr",
            )

            fpcalc_process = subprocess.Popen(
                fpcalc_command,
                stdin=ffmpeg_process.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            # fpcalc owns the duplicated read handle. Closing the parent's copy
            # lets FFmpeg observe a broken pipe if fpcalc exits early.
            ffmpeg_process.stdout.close()
            if fpcalc_process.stdout is None or fpcalc_process.stderr is None:
                raise RecognitionError("fpcalc did not expose its result pipes.")
            output_capture, output_thread = _start_binary_capture(
                cast(BinaryIO, fpcalc_process.stdout),
                name="groove-serpent-fpcalc-stdout",
            )
            fpcalc_diagnostic, fpcalc_stderr_thread = start_diagnostic_reader(
                fpcalc_process,
                name="groove-serpent-fpcalc-stderr",
            )

            try:
                fpcalc_returncode = fpcalc_process.wait(
                    timeout=self._fingerprint_timeout_seconds
                )
            except subprocess.TimeoutExpired as exc:
                raise RecognitionError(
                    "Audio fingerprinting timed out; FFmpeg and fpcalc were stopped."
                ) from exc

            try:
                ffmpeg_returncode = ffmpeg_process.wait(timeout=5.0)
            except subprocess.TimeoutExpired as exc:
                raise RecognitionError(
                    "FFmpeg did not finish after fingerprinting and was stopped."
                ) from exc

            _join_binary_capture(fpcalc_process, output_thread)
            join_diagnostic_reader(ffmpeg_process, ffmpeg_stderr_thread)
            join_diagnostic_reader(fpcalc_process, fpcalc_stderr_thread)
            if output_thread is not None and output_thread.is_alive():
                raise RecognitionError("fpcalc fingerprint output did not close cleanly.")
            ffmpeg_error = ffmpeg_diagnostic.text() if ffmpeg_diagnostic else ""
            fpcalc_error = fpcalc_diagnostic.text() if fpcalc_diagnostic else ""

            if ffmpeg_returncode != 0:
                raise RecognitionError(
                    "FFmpeg could not prepare the recognition excerpt"
                    + (f": {ffmpeg_error}" if ffmpeg_error else ".")
                )
            if fpcalc_returncode != 0:
                raise RecognitionError(
                    "fpcalc could not fingerprint the recognition excerpt"
                    + (f": {fpcalc_error}" if fpcalc_error else ".")
                )
            if output_capture is None or output_capture.truncated:
                raise RecognitionError("fpcalc fingerprint output exceeded its bound.")

            # fpcalc 1.6 can report duration 0 for a valid non-seekable WAV
            # stream because the pipe's WAV header has no final byte length.
            # Exact sample boundaries give us the authoritative duration.
            return _parse_fingerprint_output(
                output_capture.payload(),
                authoritative_duration=authoritative_duration,
            )
        except OSError as exc:
            raise RecognitionError(f"Could not start the recognition runtime: {exc}") from exc
        finally:
            if ffmpeg_process is not None and ffmpeg_process.stdout is not None:
                try:
                    ffmpeg_process.stdout.close()
                except OSError:
                    pass
            terminate_and_reap(fpcalc_process)
            terminate_and_reap(ffmpeg_process)
            _join_binary_capture(fpcalc_process, output_thread)
            join_diagnostic_reader(ffmpeg_process, ffmpeg_stderr_thread)
            join_diagnostic_reader(fpcalc_process, fpcalc_stderr_thread)
            _assert_executable_unchanged(runtime.ffmpeg, "FFmpeg executable")
            _assert_executable_unchanged(runtime.fpcalc, "fpcalc executable")

    def _lookup(self, *, fingerprint: str, duration: int) -> dict[str, object]:
        if not fingerprint:
            raise RecognitionError("The fingerprint backend returned an empty fingerprint.")
        if duration <= 0:
            raise RecognitionError("The fingerprint duration is invalid.")

        form = urllib.parse.urlencode(
            {
                "fingerprint": fingerprint,
                "duration": str(duration),
                "client": self._api_key,
                "meta": "recordings+releasegroups+releases",
            }
        ).encode("ascii")
        request = urllib.request.Request(
            ACOUSTID_LOOKUP_URL,
            data=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": __user_agent__,
            },
            method="POST",
        )

        self._wait_for_request_slot()
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_seconds
            ) as response:
                raw = response.read(self._max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc, self._max_response_bytes)
            suffix = f": {detail}" if detail else ""
            raise RecognitionError(
                f"AcoustID lookup failed with HTTP {exc.code}{suffix}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RecognitionError(f"AcoustID lookup could not connect: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RecognitionError("AcoustID lookup timed out.") from exc
        except OSError as exc:
            raise RecognitionError(f"AcoustID lookup failed: {exc}") from exc

        if len(raw) > self._max_response_bytes:
            raise RecognitionError("AcoustID returned an unexpectedly large response.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecognitionError("AcoustID returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise RecognitionError("AcoustID returned an invalid response object.")

        status = payload.get("status")
        if status != "ok":
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("code") or "unknown error")
            else:
                message = str(error or status or "unknown error")
            raise RecognitionError(f"AcoustID rejected the lookup: {message}")
        return payload

    def _wait_for_request_slot(self) -> None:
        # Starting requests at least 1/3 second apart keeps this process at or
        # below AcoustID's documented three-requests-per-second ceiling.
        global _acoustid_last_request_started

        minimum_interval = 1.0 / 3.0
        with _ACOUSTID_RATE_LOCK:
            now = time.monotonic()
            wait_seconds = _acoustid_last_request_started + minimum_interval - now
            if _acoustid_last_request_started and wait_seconds > 0:
                time.sleep(wait_seconds)
                now = time.monotonic()
            _acoustid_last_request_started = max(
                now, _acoustid_last_request_started + minimum_interval
            )


def _find_fpcalc() -> str | None:
    configured = os.environ.get("GROOVE_SERPENT_FPCALC", "").strip()
    if configured:
        resolved = find_executable("fpcalc", explicit=configured)
        if resolved:
            return resolved

    resolved = find_executable("fpcalc")
    if resolved:
        return resolved

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            packages = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
            if packages.is_dir():
                matches: list[Path] = []
                for package in packages.glob("AcoustID.Chromaprint_*"):
                    if package.is_dir():
                        matches.extend(package.rglob("fpcalc.exe"))
                for match in sorted(matches, key=lambda item: str(item).casefold()):
                    if match.is_file():
                        return str(match.resolve())
    return None


def _recognition_speed_details(
    sample_rate: int,
    source_speed_factor: float,
) -> tuple[int, float]:
    """Return the exact integer-rate correction used before fingerprinting."""

    try:
        return speed_correction_details(sample_rate, source_speed_factor)
    except ProjectValidationError as exc:
        raise ValueError(str(exc)) from exc


def _excerpt_sample_bounds(
    start_sample: int,
    end_sample: int,
    sample_rate: int,
    *,
    source_speed_factor: float = 1.0,
) -> tuple[int, int]:
    if start_sample < 0 or end_sample <= start_sample:
        raise ValueError("Fingerprint sample geometry is invalid.")
    asetrate_hz, _effective_factor = _recognition_speed_details(
        sample_rate,
        source_speed_factor,
    )
    total_seconds = (end_sample - start_sample) / asetrate_hz
    skip_seconds = min(
        _LEAD_AMBIENCE_SECONDS,
        max(0.0, total_seconds - _MIN_AUDIO_AFTER_SKIP_SECONDS),
    )
    skip_samples = math.floor(skip_seconds * asetrate_hz + 0.5)
    excerpt_start = start_sample + skip_samples
    maximum_samples = int(_MAX_FINGERPRINT_SECONDS * asetrate_hz)
    excerpt_end = min(end_sample, excerpt_start + maximum_samples)
    return excerpt_start, excerpt_end


def _diagnostic_text(value: object) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    elif value is None:
        return ""
    else:
        text = str(value)
    return " ".join(text.strip().split())[:2000]


def _fingerprint_duration(
    excerpt_start: int,
    excerpt_end: int,
    sample_rate: int,
    *,
    source_speed_factor: float = 1.0,
) -> int:
    if excerpt_start < 0 or excerpt_end <= excerpt_start:
        raise ValueError("Fingerprint sample geometry is invalid.")
    asetrate_hz, _effective_factor = _recognition_speed_details(
        sample_rate,
        source_speed_factor,
    )
    sample_count = excerpt_end - excerpt_start
    rounded = (sample_count + asetrate_hz // 2) // asetrate_hz
    return max(1, min(int(_MAX_FINGERPRINT_SECONDS), rounded))


def _validated_fingerprint(value: object, backend: str) -> str:
    if not isinstance(value, str):
        raise RecognitionError(f"{backend} returned a non-text fingerprint.")
    fingerprint = value
    if (
        not fingerprint
        or fingerprint != fingerprint.strip()
        or len(fingerprint) > _MAX_FINGERPRINT_CHARACTERS
        or _FINGERPRINT_RE.fullmatch(fingerprint) is None
    ):
        raise RecognitionError(f"{backend} returned an invalid fingerprint.")
    return fingerprint


def _parse_direct_fingerprint(raw: bytes) -> str:
    if len(raw) > _MAX_FINGERPRINT_OUTPUT_BYTES:
        raise RecognitionError("FFmpeg fingerprint output exceeded its bound.")
    try:
        text = raw.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise RecognitionError("FFmpeg returned a non-ASCII fingerprint.") from exc
    return _validated_fingerprint(text, "FFmpeg")


def _parse_fingerprint_output(
    raw: object, *, authoritative_duration: int
) -> _FingerprintPayload:
    if isinstance(raw, bytes):
        if len(raw) > _MAX_FINGERPRINT_OUTPUT_BYTES:
            raise RecognitionError("fpcalc fingerprint output exceeded its bound.")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RecognitionError("fpcalc returned non-UTF-8 JSON.") from exc
    else:
        text = str(raw or "")

    def object_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite value: {value}")

    try:
        payload = json.loads(
            text,
            object_pairs_hook=object_hook,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise RecognitionError("fpcalc returned invalid JSON.") from exc
    if not isinstance(payload, dict) or set(payload) != {"duration", "fingerprint"}:
        raise RecognitionError("fpcalc returned an invalid result object.")
    if authoritative_duration <= 0:
        raise RecognitionError("The authoritative fingerprint duration is invalid.")
    backend_duration = payload["duration"]
    if (
        isinstance(backend_duration, bool)
        or not isinstance(backend_duration, (int, float))
        or not math.isfinite(float(backend_duration))
        or float(backend_duration) < 0
    ):
        raise RecognitionError("fpcalc returned invalid duration metadata.")
    fingerprint = _validated_fingerprint(payload.get("fingerprint"), "fpcalc")
    return {"fingerprint": fingerprint, "duration": authoritative_duration}


def _http_error_detail(error: urllib.error.HTTPError, limit: int) -> str:
    try:
        raw = error.read(min(limit, _MAX_DIAGNOSTIC_BYTES) + 1)
    except OSError:
        return ""
    if len(raw) > min(limit, _MAX_DIAGNOSTIC_BYTES):
        return "response was too large"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _diagnostic_text(raw)
    if isinstance(payload, dict):
        detail = payload.get("error")
        if isinstance(detail, dict):
            return _diagnostic_text(detail.get("message") or detail.get("code"))
        return _diagnostic_text(detail)
    return ""


def _parse_matches(payload: dict[str, object]) -> list[RecognitionMatch]:
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []

    matches: dict[tuple[str, str, str], RecognitionMatch] = {}
    for raw_result in raw_results:
        if not isinstance(raw_result, dict):
            continue
        try:
            result_score = float(raw_result.get("score", 0.0))
        except (TypeError, ValueError, OverflowError):
            result_score = 0.0
        if not math.isfinite(result_score):
            result_score = 0.0
        result_score = min(1.0, max(0.0, result_score))
        recordings = raw_result.get("recordings")
        if not isinstance(recordings, list):
            continue

        for recording in recordings:
            if not isinstance(recording, dict):
                continue
            title = _nonempty_string(recording.get("title"))
            if title is None:
                continue
            recording_mbid = _nonempty_string(recording.get("id"))
            artist_credit = _join_artist_credit(recording.get("artists"))
            releases, group_ids = _release_metadata(recording)
            match = RecognitionMatch(
                title=title,
                artist_credit=artist_credit,
                score=result_score,
                recording_mbid=recording_mbid,
                release_candidates=tuple(releases),
                release_group_ids=tuple(group_ids),
            )
            key = (
                recording_mbid or "",
                title.casefold(),
                artist_credit.casefold(),
            )
            previous = matches.get(key)
            if previous is None:
                matches[key] = match
            else:
                matches[key] = _merge_match(previous, match)

    return sorted(
        matches.values(),
        key=lambda item: (
            -item.score,
            item.title.casefold(),
            item.artist_credit.casefold(),
            item.recording_mbid or "",
        ),
    )


def _join_artist_credit(raw_artists: object) -> str:
    if not isinstance(raw_artists, list):
        return ""
    pieces: list[str] = []
    usable = [item for item in raw_artists if isinstance(item, dict)]
    for index, artist in enumerate(usable):
        name = _nonempty_string(artist.get("name"))
        if name is None:
            continue
        pieces.append(name)
        join_phrase = artist.get("joinphrase")
        if isinstance(join_phrase, str):
            pieces.append(join_phrase)
        elif index < len(usable) - 1:
            pieces.append(", ")
    return "".join(pieces).strip()


def _release_metadata(
    recording: dict[str, object],
) -> tuple[list[dict[str, object]], list[str]]:
    candidates: list[dict[str, object]] = []
    group_ids: list[str] = []
    seen_candidates: set[tuple[str, str, str]] = set()

    raw_groups = recording.get("releasegroups")
    if not isinstance(raw_groups, list):
        raw_groups = recording.get("release_groups")
    if isinstance(raw_groups, list):
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict):
                continue
            group_id = _nonempty_string(raw_group.get("id"))
            if group_id and group_id not in group_ids:
                group_ids.append(group_id)
            releases = raw_group.get("releases")
            if isinstance(releases, list):
                for release in releases:
                    if isinstance(release, dict):
                        _append_release_candidate(
                            candidates,
                            seen_candidates,
                            release,
                            release_group=raw_group,
                        )

    direct_releases = recording.get("releases")
    if isinstance(direct_releases, list):
        for release in direct_releases:
            if not isinstance(release, dict):
                continue
            embedded_group = release.get("releasegroup") or release.get("release_group")
            group = embedded_group if isinstance(embedded_group, dict) else {}
            group_id = _nonempty_string(group.get("id"))
            if group_id and group_id not in group_ids:
                group_ids.append(group_id)
            _append_release_candidate(
                candidates,
                seen_candidates,
                release,
                release_group=group,
            )
    return candidates, group_ids


def _append_release_candidate(
    destination: list[dict[str, object]],
    seen: set[tuple[str, str, str]],
    release: dict[str, object],
    *,
    release_group: dict[str, object],
) -> None:
    release_id = _nonempty_string(release.get("id")) or ""
    title = _nonempty_string(release.get("title")) or ""
    group_id = _nonempty_string(release_group.get("id")) or ""
    if not release_id and not title:
        return
    key = (release_id, title.casefold(), group_id)
    if key in seen:
        return
    seen.add(key)

    candidate: dict[str, object] = {
        "release_mbid": release_id or None,
        "title": title,
        "release_group_mbid": group_id or None,
    }
    optional_fields = {
        "country": release.get("country"),
        "date": release.get("date"),
        "status": release.get("status"),
        "release_group_title": release_group.get("title"),
        "release_group_type": release_group.get("type"),
    }
    for key_name, value in optional_fields.items():
        if isinstance(value, str) and value.strip():
            candidate[key_name] = value.strip()
    secondary_types = release_group.get("secondarytypes") or release_group.get(
        "secondary_types"
    )
    if isinstance(secondary_types, list):
        candidate["release_group_secondary_types"] = [
            item for item in secondary_types if isinstance(item, str)
        ]
    destination.append(candidate)


def _merge_match(first: RecognitionMatch, second: RecognitionMatch) -> RecognitionMatch:
    releases = [dict(item) for item in first.release_candidates]
    seen = {
        (
            str(item.get("release_mbid") or ""),
            str(item.get("title") or "").casefold(),
            str(item.get("release_group_mbid") or ""),
        )
        for item in releases
    }
    for item in second.release_candidates:
        key = (
            str(item.get("release_mbid") or ""),
            str(item.get("title") or "").casefold(),
            str(item.get("release_group_mbid") or ""),
        )
        if key not in seen:
            seen.add(key)
            releases.append(dict(item))
    group_ids = list(first.release_group_ids)
    for group_id in second.release_group_ids:
        if group_id not in group_ids:
            group_ids.append(group_id)
    winner = second if second.score > first.score else first
    return RecognitionMatch(
        title=winner.title,
        artist_credit=winner.artist_credit,
        score=max(first.score, second.score),
        recording_mbid=winner.recording_mbid,
        release_candidates=tuple(releases),
        release_group_ids=tuple(group_ids),
        provider=winner.provider,
    )


def _nonempty_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
