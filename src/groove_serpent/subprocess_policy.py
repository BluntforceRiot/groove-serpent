"""Shared bounded diagnostics and cleanup for local media subprocesses."""

from __future__ import annotations

import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol


MAX_DIAGNOSTIC_BYTES = 64 * 1024
MAX_DIAGNOSTIC_TEXT = 2_000


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    """A completed short media command with bounded in-memory output."""

    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool


class _BinaryCapture(Protocol):
    def seek(self, offset: int, whence: int = 0) -> int: ...

    def read(self, size: int = -1) -> bytes: ...


def _bounded_file_prefix(stream: _BinaryCapture, limit: int) -> tuple[bytes, bool]:
    stream.seek(0)
    captured = stream.read(limit + 1)
    return captured[:limit], len(captured) > limit


def run_bounded_capture(
    command: list[str],
    *,
    stdout_limit: int = MAX_DIAGNOSTIC_BYTES,
    stderr_limit: int = MAX_DIAGNOSTIC_BYTES,
) -> BoundedProcessResult:
    """Run a short tool query without stdin or unbounded captured output."""

    if not command:
        raise ValueError("A media command cannot be empty.")
    if stdout_limit < 1 or stderr_limit < 1:
        raise ValueError("Media output limits must be positive.")
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        completed = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
        )
        stdout, stdout_truncated = _bounded_file_prefix(stdout_file, stdout_limit)
        stderr, stderr_truncated = _bounded_file_prefix(stderr_file, stderr_limit)
    return BoundedProcessResult(
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


class BoundedDiagnostic:
    """Drain a pipe completely while retaining only a small diagnostic prefix."""

    def __init__(self, *, byte_limit: int = MAX_DIAGNOSTIC_BYTES) -> None:
        self._byte_limit = byte_limit
        self._captured = bytearray()
        self._truncated = False

    def drain(self, stream: BinaryIO) -> None:
        try:
            while True:
                chunk = stream.read(8_192)
                if not chunk:
                    break
                room = self._byte_limit - len(self._captured)
                if room > 0:
                    self._captured.extend(chunk[:room])
                if len(chunk) > room:
                    self._truncated = True
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def text(self, *, character_limit: int = MAX_DIAGNOSTIC_TEXT) -> str:
        rendered = " ".join(
            bytes(self._captured).decode("utf-8", errors="replace").strip().split()
        )
        rendered = rendered[:character_limit]
        if self._truncated:
            rendered = (rendered + " [diagnostic truncated]").strip()
        return rendered


def start_diagnostic_reader(
    process: subprocess.Popen[bytes],
    *,
    name: str,
) -> tuple[BoundedDiagnostic, threading.Thread]:
    """Start draining a process stderr pipe without permitting backpressure."""

    if process.stderr is None:
        raise RuntimeError("The media subprocess did not expose a diagnostic pipe.")
    diagnostic = BoundedDiagnostic()
    thread = threading.Thread(
        target=diagnostic.drain,
        args=(process.stderr,),
        name=name,
        daemon=True,
    )
    thread.start()
    return diagnostic, thread


def join_diagnostic_reader(
    process: subprocess.Popen[bytes] | None,
    thread: threading.Thread | None,
) -> None:
    if thread is None:
        return
    thread.join(timeout=2.0)
    if thread.is_alive() and process is not None and process.stderr is not None:
        try:
            process.stderr.close()
        except (OSError, ValueError):
            pass
        thread.join(timeout=2.0)


def terminate_and_reap(
    process: subprocess.Popen[bytes] | None,
    *,
    timeout: float = 2.0,
) -> None:
    """Terminate, then kill if needed, and always attempt to reap a child."""

    if process is None:
        return
    try:
        running = process.poll() is None
    except OSError:
        running = False
    if running:
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=timeout)
            return
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        pass


def require_ffmpeg_nostdin(command: list[str]) -> list[str]:
    """Return a command containing FFmpeg's noninteractive safety flag."""

    if not command:
        raise ValueError("An FFmpeg command cannot be empty.")
    executable = Path(command[0]).name.casefold()
    if not executable.startswith("ffmpeg"):
        raise ValueError("The media command is not FFmpeg.")
    if "-nostdin" not in command[1:]:
        command = [command[0], "-nostdin", *command[1:]]
    return command


__all__ = [
    "BoundedDiagnostic",
    "BoundedProcessResult",
    "MAX_DIAGNOSTIC_BYTES",
    "MAX_DIAGNOSTIC_TEXT",
    "join_diagnostic_reader",
    "require_ffmpeg_nostdin",
    "run_bounded_capture",
    "start_diagnostic_reader",
    "terminate_and_reap",
]
