from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import UnsupportedCaptureError
from .models import AudioSource


CAPTURE_ENVELOPE_SCHEMA = "groove-serpent.capture-envelope/1"
CAPTURE_PROFILE_ID = "lossless-vinyl-capture-v1"

SUPPORTED_SAMPLE_RATES = (44_100, 48_000, 88_200, 96_000, 176_400, 192_000)
SUPPORTED_CHANNEL_COUNTS = (1, 2)
SUPPORTED_BIT_DEPTHS = (16, 24)
MAX_CAPTURE_DURATION_SECONDS = 6 * 60 * 60
MAX_CAPTURE_SIZE_BYTES = 64 * 1024 * 1024 * 1024
MAX_DECODED_PCM_BYTES = 32 * 1024 * 1024 * 1024
MAX_ANALYSIS_WINDOWS = 2_000_000
MAX_PROFILE_ANALYSIS_RATE = 192_000
MAX_PROFILE_ANALYSIS_WINDOW_MS = 10_000
MAX_CAPTURE_SAMPLE_COUNT = MAX_CAPTURE_DURATION_SECONDS * max(SUPPORTED_SAMPLE_RATES)
_MAX_REPORT_INTEGER = (1 << 63) - 1

_CONTAINER_CODECS: dict[str, frozenset[str]] = {
    ".flac": frozenset({"flac"}),
    ".wav": frozenset({"pcm_s16le", "pcm_s24le"}),
    ".wave": frozenset({"pcm_s16le", "pcm_s24le"}),
    ".aif": frozenset({"pcm_s16be", "pcm_s24be"}),
    ".aiff": frozenset({"pcm_s16be", "pcm_s24be"}),
}

_REASON_MESSAGES = {
    "filename_invalid": "the capture filename is not valid text",
    "container_not_supported": "the capture container is not FLAC, WAV, or AIFF",
    "codec_container_not_supported": "the codec does not match a supported lossless container",
    "sample_rate_not_supported": "the sample rate is outside the declared tested set",
    "channel_count_not_supported": "only mono and stereo captures are supported",
    "bit_depth_unknown": "the lossless integer bit depth could not be established",
    "bit_depth_not_supported": "only 16-bit and 24-bit integer captures are supported",
    "duration_invalid": "the capture duration is not a positive finite value",
    "duration_exceeds_limit": "the capture exceeds the six-hour duration limit",
    "source_size_invalid": "the capture byte size is not a positive integer",
    "source_size_exceeds_limit": "the capture exceeds the 64 GiB file-size limit",
    "sample_count_missing": "an exact source sample count could not be established",
    "sample_count_invalid": "the exact source sample count is not a positive integer",
    "sample_count_exceeds_limit": "the source sample count exceeds the capture profile limit",
    "sample_count_inconsistent": "the sample count disagrees with duration and sample rate",
    "decoded_pcm_exceeds_limit": "the decoded PCM workload exceeds the 32 GiB profile limit",
    "analysis_settings_invalid": "the requested analysis rate or window length is invalid",
    "analysis_window_count_exceeds_limit": "the analysis would exceed two million RMS windows",
}


@dataclass(frozen=True, slots=True)
class CaptureEnvelopeReport:
    schema: str
    profile_id: str
    supported: bool
    reason_codes: tuple[str, ...]
    extension: str
    codec_name: str
    sample_rate: int | None
    channels: int | None
    bits_per_raw_sample: int | None
    duration_seconds: float | None
    sample_count: int | None
    size_bytes: int | None
    decoded_pcm_bytes: int | None
    analysis_rate: int | None
    analysis_window_ms: int | None
    analysis_window_count: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "profile_id": self.profile_id,
            "supported": self.supported,
            "reason_codes": list(self.reason_codes),
            "reason_messages": [_REASON_MESSAGES[reason] for reason in self.reason_codes],
            "source": {
                "extension": self.extension,
                "codec_name": self.codec_name,
                "sample_rate": self.sample_rate,
                "channels": self.channels,
                "bits_per_raw_sample": self.bits_per_raw_sample,
                "duration_seconds": self.duration_seconds,
                "sample_count": self.sample_count,
                "size_bytes": self.size_bytes,
            },
            "resource_estimate": {
                "decoded_pcm_bytes": self.decoded_pcm_bytes,
                "analysis_rate": self.analysis_rate,
                "analysis_window_ms": self.analysis_window_ms,
                "analysis_window_count": self.analysis_window_count,
            },
            "limits": {
                "sample_rates": list(SUPPORTED_SAMPLE_RATES),
                "channel_counts": list(SUPPORTED_CHANNEL_COUNTS),
                "bit_depths": list(SUPPORTED_BIT_DEPTHS),
                "max_duration_seconds": MAX_CAPTURE_DURATION_SECONDS,
                "max_size_bytes": MAX_CAPTURE_SIZE_BYTES,
                "max_decoded_pcm_bytes": MAX_DECODED_PCM_BYTES,
                "max_analysis_windows": MAX_ANALYSIS_WINDOWS,
                "max_analysis_rate": MAX_PROFILE_ANALYSIS_RATE,
                "max_analysis_window_ms": MAX_PROFILE_ANALYSIS_WINDOW_MS,
                "max_sample_count": MAX_CAPTURE_SAMPLE_COUNT,
            },
        }


def _integer_or_none(value: object) -> int | None:
    return value if type(value) is int else None


def _report_integer_or_none(value: int | None) -> int | None:
    if value is None or not -_MAX_REPORT_INTEGER <= value <= _MAX_REPORT_INTEGER:
        return None
    return value


def _finite_float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def evaluate_capture_envelope(
    source: AudioSource,
    *,
    analysis_rate: int = 8_000,
    analysis_window_ms: int = 50,
) -> CaptureEnvelopeReport:
    """Evaluate a probed source against the deliberately narrow 1.0 profile.

    This is a metadata and bounded-workload gate, not a decoder proof. The
    caller must still use a verified source snapshot and FFmpeg decode checks.
    """

    reasons: list[str] = []
    filename = source.filename
    if (
        not isinstance(filename, str)
        or not filename
        or Path(filename).name != filename
        or any(ord(character) < 32 for character in filename)
    ):
        extension = ""
        reasons.append("filename_invalid")
    else:
        extension = Path(filename).suffix.casefold()

    codecs = _CONTAINER_CODECS.get(extension)
    if codecs is None:
        reasons.append("container_not_supported")
    if not isinstance(source.codec_name, str) or codecs is None or source.codec_name not in codecs:
        reasons.append("codec_container_not_supported")

    raw_sample_rate = _integer_or_none(source.sample_rate)
    sample_rate = _report_integer_or_none(raw_sample_rate)
    if raw_sample_rate not in SUPPORTED_SAMPLE_RATES:
        reasons.append("sample_rate_not_supported")

    raw_channels = _integer_or_none(source.channels)
    channels = _report_integer_or_none(raw_channels)
    if raw_channels not in SUPPORTED_CHANNEL_COUNTS:
        reasons.append("channel_count_not_supported")

    raw_bits = _integer_or_none(source.bits_per_raw_sample)
    bits = _report_integer_or_none(raw_bits)
    if raw_bits is None:
        reasons.append("bit_depth_unknown")
    elif raw_bits not in SUPPORTED_BIT_DEPTHS:
        reasons.append("bit_depth_not_supported")

    duration = _finite_float_or_none(source.duration_seconds)
    if duration is None or duration <= 0:
        reasons.append("duration_invalid")
    elif duration > MAX_CAPTURE_DURATION_SECONDS:
        reasons.append("duration_exceeds_limit")

    raw_size_bytes = _integer_or_none(source.size_bytes)
    size_bytes = _report_integer_or_none(raw_size_bytes)
    if raw_size_bytes is None or raw_size_bytes <= 0:
        reasons.append("source_size_invalid")
    elif raw_size_bytes > MAX_CAPTURE_SIZE_BYTES:
        reasons.append("source_size_exceeds_limit")

    raw_sample_count = _integer_or_none(source.sample_count)
    sample_count = _report_integer_or_none(raw_sample_count)
    if source.sample_count is None:
        reasons.append("sample_count_missing")
    elif raw_sample_count is None or raw_sample_count <= 0:
        reasons.append("sample_count_invalid")
    elif raw_sample_count > MAX_CAPTURE_SAMPLE_COUNT:
        reasons.append("sample_count_exceeds_limit")
        sample_count = None
    elif (
        sample_count is not None
        and sample_rate in SUPPORTED_SAMPLE_RATES
        and duration is not None
        and 0 < duration <= MAX_CAPTURE_DURATION_SECONDS
    ):
        expected_count = round(duration * sample_rate)
        if abs(sample_count - expected_count) > 2:
            reasons.append("sample_count_inconsistent")

    decoded_pcm_bytes: int | None = None
    if sample_count is not None and sample_count > 0 and channels is not None and bits is not None:
        decoded_pcm_bytes = sample_count * channels * ((bits + 7) // 8)
        if decoded_pcm_bytes > MAX_DECODED_PCM_BYTES:
            reasons.append("decoded_pcm_exceeds_limit")

    raw_analysis_rate = _integer_or_none(analysis_rate)
    raw_window_ms = _integer_or_none(analysis_window_ms)
    normalized_analysis_rate = _report_integer_or_none(raw_analysis_rate)
    normalized_window_ms = _report_integer_or_none(raw_window_ms)
    analysis_window_count: int | None = None
    if (
        normalized_analysis_rate is None
        or not 1 <= normalized_analysis_rate <= MAX_PROFILE_ANALYSIS_RATE
        or normalized_window_ms is None
        or not 1 <= normalized_window_ms <= MAX_PROFILE_ANALYSIS_WINDOW_MS
    ):
        reasons.append("analysis_settings_invalid")
    elif duration is not None and 0 < duration <= MAX_CAPTURE_DURATION_SECONDS:
        window_samples = max(
            1,
            (normalized_analysis_rate * normalized_window_ms + 500) // 1_000,
        )
        analysis_samples = math.ceil(duration * normalized_analysis_rate)
        analysis_window_count = math.ceil(analysis_samples / window_samples)
        if analysis_window_count > MAX_ANALYSIS_WINDOWS:
            reasons.append("analysis_window_count_exceeds_limit")

    reason_codes = tuple(dict.fromkeys(reasons))
    return CaptureEnvelopeReport(
        schema=CAPTURE_ENVELOPE_SCHEMA,
        profile_id=CAPTURE_PROFILE_ID,
        supported=not reason_codes,
        reason_codes=reason_codes,
        extension=extension,
        codec_name=(source.codec_name if isinstance(source.codec_name, str) else ""),
        sample_rate=sample_rate,
        channels=channels,
        bits_per_raw_sample=bits,
        duration_seconds=duration,
        sample_count=sample_count,
        size_bytes=size_bytes,
        decoded_pcm_bytes=decoded_pcm_bytes,
        analysis_rate=normalized_analysis_rate,
        analysis_window_ms=normalized_window_ms,
        analysis_window_count=analysis_window_count,
    )


def require_supported_capture(
    source: AudioSource,
    *,
    analysis_rate: int = 8_000,
    analysis_window_ms: int = 50,
) -> CaptureEnvelopeReport:
    report = evaluate_capture_envelope(
        source,
        analysis_rate=analysis_rate,
        analysis_window_ms=analysis_window_ms,
    )
    if report.supported:
        return report
    details = "; ".join(f"{reason}: {_REASON_MESSAGES[reason]}" for reason in report.reason_codes)
    raise UnsupportedCaptureError(
        f"Capture is outside {CAPTURE_PROFILE_ID}: {details}. "
        "The source was not analyzed or modified."
    )
