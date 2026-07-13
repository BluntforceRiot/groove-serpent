from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypedDict, runtime_checkable

from . import __user_agent__
from .audio_snapshot import VerifiedAudioSnapshot, verified_audio_snapshot
from .errors import GrooveSerpentError
from .subprocess_policy import (
    BoundedDiagnostic,
    MAX_DIAGNOSTIC_BYTES,
    join_diagnostic_reader,
    start_diagnostic_reader,
    terminate_and_reap,
)

_MAX_DIAGNOSTIC_BYTES = MAX_DIAGNOSTIC_BYTES


ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
_MAX_FINGERPRINT_SECONDS = 120.0
_LEAD_AMBIENCE_SECONDS = 8.0
_MIN_AUDIO_AFTER_SKIP_SECONDS = 15.0
_FP_SAMPLE_RATE = 11_025
_ACOUSTID_RATE_LOCK = threading.Lock()
_acoustid_last_request_started = 0.0


class _FingerprintPayload(TypedDict):
    fingerprint: str
    duration: int


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
    ) -> list[RecognitionMatch]:
        """Identify audio within exact source-sample boundaries."""


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
    ) -> list[RecognitionMatch]:
        del source_path, start_sample, end_sample, sample_rate
        return []


class AcoustIDRecognitionProvider:
    """Optional AcoustID provider using FFmpeg and Chromaprint's ``fpcalc``."""

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
        try:
            fpcalc = _find_fpcalc()
        except (OSError, ValueError):
            fpcalc = None
        try:
            ffmpeg = shutil.which("ffmpeg")
        except (OSError, ValueError):
            ffmpeg = None
        if fpcalc is None:
            missing.append("fpcalc")
        if ffmpeg is None:
            missing.append("ffmpeg")

        if missing:
            labels = {
                "api_key": "an AcoustID API key",
                "fpcalc": "the Chromaprint fpcalc executable",
                "ffmpeg": "FFmpeg",
            }
            readable = ", ".join(labels[item] for item in missing)
            return RecognitionReadiness(
                provider=self.name,
                enabled=True,
                ready=False,
                message=f"AcoustID recognition is unavailable: missing {readable}.",
                missing=tuple(missing),
            )

        return RecognitionReadiness(
            provider=self.name,
            enabled=True,
            ready=True,
            message="AcoustID recognition is ready.",
        )

    def identify_track(
        self,
        source_path: str | Path | VerifiedAudioSnapshot,
        start_sample: int,
        end_sample: int,
        sample_rate: int,
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
                )

        snapshot = source_path
        snapshot.assert_snapshot_unchanged()
        path = snapshot.path

        fpcalc = _find_fpcalc()
        ffmpeg = shutil.which("ffmpeg")
        # The readiness check is deliberately non-raising, but a runtime can be
        # removed between that check and use. Keep that race user-friendly.
        if fpcalc is None or ffmpeg is None:
            raise RecognitionError(
                "Recognition runtime became unavailable; check FFmpeg and fpcalc."
            )

        excerpt_start, excerpt_end = _excerpt_sample_bounds(
            start_sample, end_sample, sample_rate
        )
        fingerprint_payload = self._fingerprint(
            path,
            excerpt_start,
            excerpt_end,
            sample_rate,
            ffmpeg=ffmpeg,
            fpcalc=fpcalc,
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
        ffmpeg: str,
        fpcalc: str,
    ) -> _FingerprintPayload:
        excerpt_seconds = (excerpt_end - excerpt_start) / sample_rate
        length_seconds = max(1, min(120, int(math.ceil(excerpt_seconds))))
        filter_graph = (
            f"atrim=start_sample={excerpt_start}:end_sample={excerpt_end},"
            "asetpts=PTS-STARTPTS"
        )
        ffmpeg_command = [
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
            fpcalc,
            "-json",
            "-length",
            str(length_seconds),
            "-",
        ]

        ffmpeg_process: subprocess.Popen[bytes] | None = None
        fpcalc_process: subprocess.Popen[bytes] | None = None
        ffmpeg_diagnostic: BoundedDiagnostic | None = None
        stderr_thread: threading.Thread | None = None
        try:
            ffmpeg_process = subprocess.Popen(
                ffmpeg_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if ffmpeg_process.stdout is None or ffmpeg_process.stderr is None:
                raise RecognitionError("FFmpeg did not expose the required audio pipe.")

            ffmpeg_diagnostic, stderr_thread = start_diagnostic_reader(
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

            try:
                fpcalc_stdout, fpcalc_stderr = fpcalc_process.communicate(
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

            join_diagnostic_reader(ffmpeg_process, stderr_thread)
            ffmpeg_error = ffmpeg_diagnostic.text() if ffmpeg_diagnostic else ""
            fpcalc_error = _diagnostic_text(fpcalc_stderr)

            if ffmpeg_returncode != 0:
                raise RecognitionError(
                    "FFmpeg could not prepare the recognition excerpt"
                    + (f": {ffmpeg_error}" if ffmpeg_error else ".")
                )
            if fpcalc_process.returncode != 0:
                raise RecognitionError(
                    "fpcalc could not fingerprint the recognition excerpt"
                    + (f": {fpcalc_error}" if fpcalc_error else ".")
                )

            # fpcalc 1.6 can report duration 0 for a valid non-seekable WAV
            # stream because the pipe's WAV header has no final byte length.
            # Exact sample boundaries give us the authoritative duration.
            return _parse_fingerprint_output(
                fpcalc_stdout,
                fallback_duration=max(1, int(round(excerpt_seconds))),
            )
        except OSError as exc:
            raise RecognitionError(f"Could not start the recognition runtime: {exc}") from exc
        finally:
            if ffmpeg_process is not None and ffmpeg_process.stdout is not None:
                try:
                    ffmpeg_process.stdout.close()
                except OSError:
                    pass
            # communicate()/wait() may already have reaped either child. These
            # calls are idempotent and cover every exceptional exit as well.
            terminate_and_reap(fpcalc_process)
            terminate_and_reap(ffmpeg_process)
            join_diagnostic_reader(ffmpeg_process, stderr_thread)

    def _lookup(self, *, fingerprint: str, duration: int) -> dict[str, object]:
        if not fingerprint:
            raise RecognitionError("fpcalc returned an empty fingerprint.")
        if duration <= 0:
            raise RecognitionError("fpcalc returned an invalid audio duration.")

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
        candidate = Path(os.path.expandvars(configured)).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        resolved = shutil.which(configured)
        if resolved:
            return resolved

    resolved = shutil.which("fpcalc")
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


def _excerpt_sample_bounds(
    start_sample: int, end_sample: int, sample_rate: int
) -> tuple[int, int]:
    total_seconds = (end_sample - start_sample) / sample_rate
    skip_seconds = min(
        _LEAD_AMBIENCE_SECONDS,
        max(0.0, total_seconds - _MIN_AUDIO_AFTER_SKIP_SECONDS),
    )
    excerpt_start = start_sample + int(round(skip_seconds * sample_rate))
    maximum_samples = int(_MAX_FINGERPRINT_SECONDS * sample_rate)
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


def _parse_fingerprint_output(
    raw: object, *, fallback_duration: int | None = None
) -> _FingerprintPayload:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw or "")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecognitionError("fpcalc returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise RecognitionError("fpcalc returned an invalid result object.")
    fingerprint = payload.get("fingerprint")
    try:
        duration = int(round(float(payload.get("duration", 0))))
    except (TypeError, ValueError, OverflowError) as exc:
        raise RecognitionError("fpcalc returned an invalid audio duration.") from exc
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise RecognitionError("fpcalc returned an empty fingerprint.")
    if duration <= 0 and fallback_duration is not None and fallback_duration > 0:
        duration = fallback_duration
    if duration <= 0:
        raise RecognitionError("fpcalc returned an invalid audio duration.")
    return {"fingerprint": fingerprint, "duration": duration}


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
