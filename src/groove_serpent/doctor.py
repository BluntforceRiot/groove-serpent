"""Machine-readable local capability checks for Groove Serpent."""

from __future__ import annotations

import platform
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .atomic_create import probe_atomic_no_replace
from .audacity import discover_audacity
from .errors import DependencyError
from .media import find_tool, tool_version
from .recognition import AcoustIDRecognitionProvider, fingerprint_backend_readiness
from .subprocess_policy import run_bounded_capture


DOCTOR_SCHEMA = "groove-serpent.doctor/1"


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    """One explicit required or optional local capability result."""

    capability: str
    required: bool
    status: str
    message: str
    executable: str = ""
    version: str = ""
    backend: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _tool_check(name: str, *, required: bool) -> CapabilityCheck:
    try:
        executable = find_tool(name)
        version = tool_version(name)
    except (DependencyError, OSError, RuntimeError, ValueError) as exc:
        return CapabilityCheck(
            capability=name,
            required=required,
            status="missing",
            message=str(exc),
        )
    return CapabilityCheck(
        capability=name,
        required=required,
        status="ready",
        message=f"{name} is available.",
        executable=executable,
        version=version,
    )


def _soxr_check() -> CapabilityCheck:
    """Exercise the exact resampler backend used for fixed-speed correction."""

    try:
        ffmpeg = find_tool("ffmpeg")
    except (DependencyError, OSError, RuntimeError, ValueError) as exc:
        return CapabilityCheck(
            capability="ffmpeg-libsoxr",
            required=True,
            status="missing",
            message=str(exc),
        )
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-t",
        "0.02",
        "-af",
        "aresample=44100:resampler=soxr",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = run_bounded_capture(command)
    except (OSError, RuntimeError, ValueError) as exc:
        return CapabilityCheck(
            capability="ffmpeg-libsoxr",
            required=True,
            status="missing",
            message=f"The libsoxr smoke test could not run: {exc}",
            executable=ffmpeg,
        )
    if completed.returncode != 0:
        diagnostic = completed.stderr.decode("utf-8", errors="replace")
        diagnostic = " ".join(diagnostic.split())[:500]
        message = "FFmpeg could not execute a libsoxr resample."
        if diagnostic:
            message += f" {diagnostic}"
        return CapabilityCheck(
            capability="ffmpeg-libsoxr",
            required=True,
            status="missing",
            message=message,
            executable=ffmpeg,
        )
    return CapabilityCheck(
        capability="ffmpeg-libsoxr",
        required=True,
        status="ready",
        message="FFmpeg completed the exact libsoxr resampler smoke path.",
        executable=ffmpeg,
    )


def _optional_checks() -> list[CapabilityCheck]:
    fingerprinting = fingerprint_backend_readiness()
    recognition = AcoustIDRecognitionProvider().readiness()
    audacity = discover_audacity()
    return [
        CapabilityCheck(
            capability="acoustic-fingerprinting",
            required=False,
            status="ready" if fingerprinting.ready else "optional-unavailable",
            message=fingerprinting.message,
            executable=fingerprinting.ffmpeg,
            backend=fingerprinting.backend,
        ),
        CapabilityCheck(
            capability="acoustic-identification",
            required=False,
            status="ready" if recognition.ready else "optional-unavailable",
            message=recognition.message,
        ),
        CapabilityCheck(
            capability="audacity-script-pipe",
            required=False,
            status=("ready" if audacity.script_pipe_enabled else "optional-unavailable"),
            message=audacity.message,
            executable=audacity.executable,
        ),
    ]


def _atomic_filesystem_check(path: Path) -> CapabilityCheck:
    try:
        exercised = probe_atomic_no_replace(path)
    except (OSError, ValueError) as exc:
        return CapabilityCheck(
            capability="atomic-no-replace-filesystem",
            required=True,
            status="missing",
            message=(
                f"The destination filesystem cannot safely create new mutable "
                f"files without replacement: {exc}"
            ),
        )
    return CapabilityCheck(
        capability="atomic-no-replace-filesystem",
        required=True,
        status="ready",
        message=f"Atomic no-replace creation passed in {exercised}.",
    )


def build_doctor_report(
    *,
    required_checks: tuple[Callable[[], CapabilityCheck], ...] | None = None,
    destination_path: Path | None = None,
) -> dict[str, Any]:
    """Return strict JSON-ready capability evidence without changing configuration."""

    required_results = [
        check()
        for check in (
            required_checks
            if required_checks is not None
            else (
                lambda: _tool_check("ffmpeg", required=True),
                lambda: _tool_check("ffprobe", required=True),
                _soxr_check,
            )
        )
    ]
    if destination_path is not None:
        required_results.append(_atomic_filesystem_check(destination_path))
    checks = required_results + _optional_checks()
    required_ready = all(
        check.status == "ready" for check in checks if check.required
    )
    return {
        "schema": DOCTOR_SCHEMA,
        "groove_serpent_version": __version__,
        "ready": required_ready,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
        },
        "checks": [check.to_dict() for check in checks],
    }


__all__ = [
    "CapabilityCheck",
    "DOCTOR_SCHEMA",
    "build_doctor_report",
]
