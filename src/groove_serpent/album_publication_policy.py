"""Authoritative production policy for album publication-plan tool bindings."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .album_publication_plan import ToolBinding
from .errors import ExportError, ProjectValidationError
from .media import find_tool
from .publication import assert_file_receipt, capture_file_receipt
from .subprocess_policy import require_ffmpeg_nostdin, run_bounded_capture
from .validation import strict_finite_number


PUBLICATION_POLICY_VERSION = "groove-serpent.album-publication-policy/1"
OUTPUT_LAYOUT_POLICY_VERSION = "groove-serpent.album-output-layout/1"
NAMING_POLICY_VERSION = "groove-serpent.album-portable-names/1"

SUPPORTED_OPERATIONS = (
    "source-side",
    "restore-side",
    "correct-speed-side",
    "assemble-archival",
    "assemble-restored",
    "encode-lossless",
    "encode-portable",
)


@dataclass(frozen=True, slots=True)
class PublicationSettings:
    flac_compression: int = 8
    aac_bitrate_kbps: int = 256

    def validate(self) -> None:
        if type(self.flac_compression) is not int or not 0 <= self.flac_compression <= 12:
            raise ProjectValidationError(
                "FLAC compression must be a JSON integer between 0 and 12."
            )
        if (
            type(self.aac_bitrate_kbps) is not int
            or not 64 <= self.aac_bitrate_kbps <= 512
        ):
            raise ProjectValidationError(
                "AAC bitrate must be a JSON integer between 64 and 512 kbps."
            )


@dataclass(frozen=True, slots=True)
class ToolObservations:
    groove_serpent_version: str
    ffmpeg_version: str
    ffprobe_version: str
    ffmpeg_executable_sha256: str
    ffprobe_executable_sha256: str
    ffmpeg_version_output_sha256: str
    ffprobe_version_output_sha256: str

    def validate(self) -> None:
        for value, label in (
            (self.groove_serpent_version, "Groove Serpent version"),
            (self.ffmpeg_version, "FFmpeg version"),
            (self.ffprobe_version, "ffprobe version"),
        ):
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 128
                or any(ord(character) < 32 for character in value)
            ):
                raise ProjectValidationError(
                    f"{label} must be 1-128 characters of trimmed printable text."
                )
        for value, label in (
            (self.ffmpeg_executable_sha256, "FFmpeg executable SHA-256"),
            (self.ffprobe_executable_sha256, "ffprobe executable SHA-256"),
            (self.ffmpeg_version_output_sha256, "FFmpeg version-output SHA-256"),
            (self.ffprobe_version_output_sha256, "ffprobe version-output SHA-256"),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ProjectValidationError(
                    f"{label} must be 64 lowercase hexadecimal characters."
                )


def _observe_media_tool(name: str) -> tuple[str, str, str]:
    executable = find_tool(name)
    executable_path = Path(executable).resolve()
    try:
        receipt = capture_file_receipt(
            executable_path,
            label=f"{name} executable",
        )
        # Execute the exact resolved file whose bytes are bound below.  Using the
        # original PATH result would let a symlink/reparse alias select a
        # different executable after its target was captured.
        command = [str(executable_path), "-version"]
        if name == "ffmpeg":
            command = require_ffmpeg_nostdin(command)
        completed = run_bounded_capture(command)
        if (
            completed.returncode != 0
            or completed.stdout_truncated
            or completed.stderr_truncated
        ):
            raise ProjectValidationError(
                f"{name} version/build output could not be captured completely."
            )
        output = completed.stdout or completed.stderr
        lines = output.decode("utf-8", errors="replace").splitlines()
        if not lines:
            raise ProjectValidationError(f"{name} returned no version/build output.")
        version = lines[0].strip()
        output_sha256 = hashlib.sha256(
            b"stdout\0"
            + completed.stdout
            + b"\0stderr\0"
            + completed.stderr
        ).hexdigest()
        assert_file_receipt(
            executable_path,
            receipt,
            label=f"{name} executable",
        )
    except ExportError as exc:
        raise ProjectValidationError(
            f"{name} changed while its publication observation was captured."
        ) from exc
    return version, receipt.sha256, output_sha256


def observe_publication_tools() -> ToolObservations:
    ffmpeg_version, ffmpeg_executable, ffmpeg_output = _observe_media_tool("ffmpeg")
    ffprobe_version, ffprobe_executable, ffprobe_output = _observe_media_tool(
        "ffprobe"
    )
    observations = ToolObservations(
        groove_serpent_version=__version__,
        ffmpeg_version=ffmpeg_version,
        ffprobe_version=ffprobe_version,
        ffmpeg_executable_sha256=ffmpeg_executable,
        ffprobe_executable_sha256=ffprobe_executable,
        ffmpeg_version_output_sha256=ffmpeg_output,
        ffprobe_version_output_sha256=ffprobe_output,
    )
    observations.validate()
    return observations


def speed_correction_details(
    source_sample_rate: int,
    requested_speed_factor: float,
) -> tuple[int, float]:
    if type(source_sample_rate) is not int or not 1 <= source_sample_rate <= 768_000:
        raise ProjectValidationError(
            "Source sample rate must be an integer between 1 and 768000 Hz."
        )
    factor = strict_finite_number(requested_speed_factor, "Requested speed factor")
    if not 0.25 <= factor <= 2.0:
        raise ProjectValidationError(
            "Requested speed factor must be between 0.25 and 2.0."
        )
    asetrate_hz = math.floor(source_sample_rate / factor + 0.5)
    if asetrate_hz < 1:
        raise ProjectValidationError("Requested speed factor produced an invalid asetrate.")
    return asetrate_hz, source_sample_rate / asetrate_hz


def _common_configuration(
    operation: str,
    observations: ToolObservations,
) -> dict[str, Any]:
    observations.validate()
    if operation not in SUPPORTED_OPERATIONS:
        raise ProjectValidationError(f"Unsupported publication operation {operation!r}.")
    return {
        "operation": operation,
        "policy_version": PUBLICATION_POLICY_VERSION,
        "output_layout_policy_version": OUTPUT_LAYOUT_POLICY_VERSION,
        "naming_policy_version": NAMING_POLICY_VERSION,
        "groove_serpent_version": observations.groove_serpent_version,
        "ffmpeg_version": observations.ffmpeg_version,
        "ffprobe_version": observations.ffprobe_version,
        "ffmpeg_executable_sha256": observations.ffmpeg_executable_sha256,
        "ffprobe_executable_sha256": observations.ffprobe_executable_sha256,
        "ffmpeg_version_output_sha256": observations.ffmpeg_version_output_sha256,
        "ffprobe_version_output_sha256": observations.ffprobe_version_output_sha256,
    }


def operation_configuration(
    operation: str,
    settings: PublicationSettings,
    observations: ToolObservations,
    *,
    source_sample_rate: int | None = None,
    requested_speed_factor: float | None = None,
    restoration_mode: str | None = None,
) -> dict[str, Any]:
    """Return the exact allowlisted configuration for one production node."""

    settings.validate()
    configuration = _common_configuration(operation, observations)
    if operation == "source-side":
        configuration.update(
            {
                "artifact_role": "immutable-source-input",
                "copy_mode": "verified-byte-identical",
                "content_range": "full-capture",
            }
        )
    elif operation == "restore-side":
        configuration.update(
            {
                "artifact_role": "restored-side-input",
                "input_mode": "validated-restoration-render",
                "content_range": "project-music-range",
                "audio_format": "flac",
                "pcm_policy": "receipt-proven-approved-windows-only",
            }
        )
    elif operation == "correct-speed-side":
        if source_sample_rate is None or requested_speed_factor is None:
            raise ProjectValidationError(
                "Speed-correction policy requires source rate and requested factor."
            )
        if restoration_mode not in {"none", "reviewed"}:
            raise ProjectValidationError(
                "Speed-correction policy requires restoration_mode 'none' or 'reviewed'."
            )
        asetrate_hz, effective_factor = speed_correction_details(
            source_sample_rate,
            requested_speed_factor,
        )
        configuration.update(
            {
                "artifact_role": "continuous-corrected-side",
                "restoration_mode": restoration_mode,
                "input_mode": (
                    "project-source-music-range"
                    if restoration_mode == "none"
                    else "validated-render-or-reviewed-clean-music-range"
                ),
                "render_mode": "continuous-corrected-side-before-track-split",
                "timeline_origin": "relative-music-start",
                "requested_speed_factor": float(requested_speed_factor),
                "effective_speed_factor": effective_factor,
                "source_sample_rate": source_sample_rate,
                "asetrate_hz": asetrate_hz,
                "asetrate_rounding": "round-half-up",
                "resampler": "libsoxr",
                "resampler_precision": 33,
                "resampler_cutoff": 0.99,
                "output_sample_rate": source_sample_rate,
                "coordinate_mapping": (
                    "round-half-up(relative_source_sample*output_rate/asetrate_hz)"
                ),
            }
        )
    elif operation == "assemble-archival":
        configuration.update(
            {
                "profile_directory": "archival-source",
                "layout_mode": "ordered-source-files",
                "copy_mode": "verified-byte-identical",
                "content_range": "full-capture",
                "concatenate": False,
                "naming_template": (
                    "{side_order:02d}-{sanitized_side_label}-{source_basename}"
                ),
            }
        )
    elif operation == "assemble-restored":
        configuration.update(
            {
                "profile_directory": "restored-side",
                "layout_mode": "ordered-music-range-side-files",
                "rendered_side_mode": "validated-restoration-render-flac",
                "clean_side_mode": "pcm-equal-music-range-flac-pass-through",
                "clean_side_flac_compression": settings.flac_compression,
                "concatenate": False,
                "naming_template": "{side_order:02d}-{sanitized_side_label}.flac",
            }
        )
    elif operation == "encode-lossless":
        configuration.update(
            {
                "profile_directory": "corrected-lossless",
                "input_mode": "continuous-corrected-side",
                "split_stage": "after-continuous-side-speed-correction",
                "coordinate_origin": "relative-music-start",
                "coordinate_rounding": "round-half-up",
                "codec": "flac",
                "flac_compression": settings.flac_compression,
                "sample_precision": "source-preserving",
                "presentation_length": "exact-mapped-track-coordinate-difference",
                "naming_template": "{album_track_number:02d}-{sanitized_title}.flac",
            }
        )
    elif operation == "encode-portable":
        configuration.update(
            {
                "profile_directory": "portable",
                "input_mode": "staged-corrected-lossless-flac",
                "codec": "aac",
                "aac_profile": "aac-lc",
                "container": "m4a",
                "bitrate_kbps": settings.aac_bitrate_kbps,
                "bitrate_bps": settings.aac_bitrate_kbps * 1_000,
                "presentation_length": "exact-corrected-lossless-input-length",
                "naming_template": "{album_track_number:02d}-{sanitized_title}.m4a",
            }
        )
    if operation != "correct-speed-side" and (
        source_sample_rate is not None
        or requested_speed_factor is not None
        or restoration_mode is not None
    ):
        raise ProjectValidationError(
            f"Operation {operation!r} does not accept speed-correction parameters."
        )
    return configuration


def operation_tool_binding(
    operation: str,
    settings: PublicationSettings,
    observations: ToolObservations,
    *,
    source_sample_rate: int | None = None,
    requested_speed_factor: float | None = None,
    restoration_mode: str | None = None,
) -> ToolBinding:
    configuration = operation_configuration(
        operation,
        settings,
        observations,
        source_sample_rate=source_sample_rate,
        requested_speed_factor=requested_speed_factor,
        restoration_mode=restoration_mode,
    )
    ffmpeg_operations = {
        "correct-speed-side",
        "assemble-restored",
        "encode-lossless",
        "encode-portable",
    }
    return ToolBinding.create(
        name="ffmpeg" if operation in ffmpeg_operations else "groove-serpent",
        version=(
            observations.ffmpeg_version
            if operation in ffmpeg_operations
            else observations.groove_serpent_version
        ),
        configuration=configuration,
    )


def validate_operation_tool_binding(
    operation: str,
    binding: ToolBinding,
    settings: PublicationSettings,
    observations: ToolObservations,
    *,
    source_sample_rate: int | None = None,
    requested_speed_factor: float | None = None,
    restoration_mode: str | None = None,
) -> None:
    expected = operation_tool_binding(
        operation,
        settings,
        observations,
        source_sample_rate=source_sample_rate,
        requested_speed_factor=requested_speed_factor,
        restoration_mode=restoration_mode,
    )
    if binding.to_dict() != expected.to_dict():
        raise ProjectValidationError(
            f"Tool binding for {operation!r} does not match production policy."
        )


__all__ = [
    "NAMING_POLICY_VERSION",
    "OUTPUT_LAYOUT_POLICY_VERSION",
    "PUBLICATION_POLICY_VERSION",
    "SUPPORTED_OPERATIONS",
    "PublicationSettings",
    "ToolObservations",
    "observe_publication_tools",
    "operation_configuration",
    "operation_tool_binding",
    "speed_correction_details",
    "validate_operation_tool_binding",
]
