"""Shared deadlines, bounded diagnostics, and cleanup for media subprocesses."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Callable


MAX_DIAGNOSTIC_BYTES = 64 * 1024
MAX_DIAGNOSTIC_TEXT = 2_000
MEDIA_QUERY_TIMEOUT_SECONDS = 30.0
MEDIA_EVIDENCE_TIMEOUT_SECONDS = 300.0
MEDIA_ANALYSIS_TIMEOUT_SECONDS = 3_600.0
MEDIA_RENDER_TIMEOUT_SECONDS = 7_200.0


_WINDOWS_JOB_WRAPPER = r"""
import ctypes
import subprocess
import sys


class _BasicLimits(ctypes.Structure):
    _fields_ = (
        ("per_process_user_time", ctypes.c_int64),
        ("per_job_user_time", ctypes.c_int64),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    )


class _IoCounters(ctypes.Structure):
    _fields_ = (
        ("read_operations", ctypes.c_uint64),
        ("write_operations", ctypes.c_uint64),
        ("other_operations", ctypes.c_uint64),
        ("read_bytes", ctypes.c_uint64),
        ("write_bytes", ctypes.c_uint64),
        ("other_bytes", ctypes.c_uint64),
    )


class _ExtendedLimits(ctypes.Structure):
    _fields_ = (
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    )


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
kernel32.CreateJobObjectW.restype = ctypes.c_void_p
kernel32.SetInformationJobObject.argtypes = (
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_uint32,
)
kernel32.SetInformationJobObject.restype = ctypes.c_int
kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
kernel32.AssignProcessToJobObject.restype = ctypes.c_int
kernel32.GetCurrentProcess.argtypes = ()
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
kernel32.CloseHandle.restype = ctypes.c_int

job = kernel32.CreateJobObjectW(None, None)
if not job:
    raise OSError(ctypes.get_last_error(), "Could not create the media process job")
limits = _ExtendedLimits()
limits.basic.limit_flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
if not kernel32.SetInformationJobObject(
    job,
    9,
    ctypes.byref(limits),
    ctypes.sizeof(limits),
):
    error = ctypes.get_last_error()
    kernel32.CloseHandle(job)
    raise OSError(error, "Could not configure the media process job")
if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
    error = ctypes.get_last_error()
    kernel32.CloseHandle(job)
    raise OSError(error, "Could not own the media process tree")

child = subprocess.Popen(sys.argv[1:])
raise SystemExit(child.wait())
"""


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    """A completed short media command with bounded in-memory output."""

    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool


class _DeadlineProcess(subprocess.Popen[bytes]):
    """A process whose owned tree is stopped after one hard wall-clock deadline."""

    def __init__(
        self,
        launched_command: list[str],
        *,
        original_command: list[str],
        media_timeout: float,
        **kwargs: Any,
    ) -> None:
        self._media_command = original_command
        self._media_timeout = media_timeout
        self._media_timed_out = threading.Event()
        self._media_scope_closed = threading.Event()
        self._media_timer: threading.Timer | None = None
        super().__init__(launched_command, **kwargs)
        timer = threading.Timer(media_timeout, self._expire_media_deadline)
        timer.name = "groove-serpent-media-deadline"
        timer.daemon = True
        self._media_timer = timer
        timer.start()

    def _expire_media_deadline(self) -> None:
        self._media_timed_out.set()
        if subprocess.Popen.poll(self) is not None:
            self._close_media_scope()
            return
        terminate_and_reap(self)

    def _close_media_scope(self) -> None:
        if os.name == "nt" or self._media_scope_closed.is_set():
            return
        self._media_scope_closed.set()
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except OSError:
            pass

    def _cancel_media_deadline(self) -> None:
        timer = self._media_timer
        if timer is not None:
            timer.cancel()
            self._media_timer = None

    def poll(self) -> int | None:
        returncode = super().poll()
        if returncode is not None:
            self._close_media_scope()
            self._cancel_media_deadline()
        return returncode

    def wait(self, timeout: float | None = None) -> int:
        returncode = super().wait(timeout=timeout)
        self._close_media_scope()
        self._cancel_media_deadline()
        if self._media_timed_out.is_set():
            raise subprocess.TimeoutExpired(self._media_command, self._media_timeout)
        return returncode


def _validate_timeout(timeout: float) -> float:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Media command timeout must be finite and positive.")
    return float(timeout)


def start_media_process(
    command: list[str],
    *,
    timeout: float,
    stdin: Any = None,
    stdout: Any = None,
    stderr: Any = None,
) -> subprocess.Popen[bytes]:
    """Start one media command in an owned process scope with a hard deadline."""

    if not command:
        raise ValueError("A media command cannot be empty.")
    finite_timeout = _validate_timeout(timeout)
    launched_command = list(command)
    kwargs: dict[str, Any] = {
        "stdin": stdin,
        "stdout": stdout,
        "stderr": stderr,
    }
    if os.name == "nt":
        # The wrapper joins its kill-on-close job before it can spawn the tool,
        # avoiding the post-Popen assignment race for tool descendants.
        launched_command = [
            sys.executable,
            "-I",
            "-c",
            _WINDOWS_JOB_WRAPPER,
            *launched_command,
        ]
    else:
        kwargs["start_new_session"] = True
    return _DeadlineProcess(
        launched_command,
        original_command=list(command),
        media_timeout=finite_timeout,
        **kwargs,
    )


def media_process_timed_out(process: subprocess.Popen[bytes]) -> bool:
    """Return whether the launcher's hard deadline stopped this process."""

    return isinstance(process, _DeadlineProcess) and process._media_timed_out.is_set()


class BoundedDiagnostic:
    """Drain a pipe while retaining only a bounded prefix."""

    def __init__(
        self,
        *,
        byte_limit: int = MAX_DIAGNOSTIC_BYTES,
        overflow_event: threading.Event | None = None,
        overflow_callback: Callable[[], None] | None = None,
    ) -> None:
        if byte_limit < 1:
            raise ValueError("Diagnostic byte limits must be positive.")
        self._byte_limit = byte_limit
        self._overflow_event = overflow_event
        self._overflow_callback = overflow_callback
        self._overflow_handled = False
        self._captured = bytearray()
        self._truncated = False

    @property
    def truncated(self) -> bool:
        return self._truncated

    def payload(self) -> bytes:
        return bytes(self._captured)

    def drain(self, stream: IO[Any]) -> None:
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
                    if self._overflow_event is not None:
                        self._overflow_event.set()
                    if (
                        self._overflow_callback is not None
                        and not self._overflow_handled
                    ):
                        self._overflow_handled = True
                        self._overflow_callback()
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def text(self, *, character_limit: int = MAX_DIAGNOSTIC_TEXT) -> str:
        rendered = " ".join(
            self.payload().decode("utf-8", errors="replace").strip().split()
        )
        rendered = rendered[:character_limit]
        if self._truncated:
            rendered = (rendered + " [diagnostic truncated]").strip()
        return rendered


def _start_reader(
    capture: BoundedDiagnostic,
    stream: IO[Any],
    *,
    name: str,
) -> threading.Thread:
    thread = threading.Thread(
        target=capture.drain,
        args=(stream,),
        name=name,
        daemon=True,
    )
    thread.start()
    return thread


def _join_reader(stream: IO[Any] | None, thread: threading.Thread | None) -> None:
    if thread is None:
        return
    thread.join(timeout=2.0)
    if thread.is_alive() and stream is not None:
        try:
            stream.close()
        except (OSError, ValueError):
            pass
        thread.join(timeout=2.0)


def run_bounded_capture(
    command: list[str],
    *,
    stdout_limit: int = MAX_DIAGNOSTIC_BYTES,
    stderr_limit: int = MAX_DIAGNOSTIC_BYTES,
    timeout: float = MEDIA_QUERY_TIMEOUT_SECONDS,
) -> BoundedProcessResult:
    """Run a short media query with deadline and generation-time output bounds."""

    if not command:
        raise ValueError("A media command cannot be empty.")
    if stdout_limit < 1 or stderr_limit < 1:
        raise ValueError("Media output limits must be positive.")
    finite_timeout = _validate_timeout(timeout)
    overflow = threading.Event()
    stdout_capture = BoundedDiagnostic(
        byte_limit=stdout_limit,
        overflow_event=overflow,
    )
    stderr_capture = BoundedDiagnostic(
        byte_limit=stderr_limit,
        overflow_event=overflow,
    )
    process: subprocess.Popen[bytes] | None = None
    stdout_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None
    try:
        process = start_media_process(
            command,
            timeout=finite_timeout,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("The media subprocess did not expose capture pipes.")
        stdout_thread = _start_reader(
            stdout_capture,
            process.stdout,
            name="groove-serpent-capture-stdout",
        )
        stderr_thread = _start_reader(
            stderr_capture,
            process.stderr,
            name="groove-serpent-capture-stderr",
        )
        while process.poll() is None:
            if overflow.wait(timeout=0.05):
                terminate_and_reap(process)
                break
        returncode = process.wait()
    finally:
        if process is not None and process.poll() is None:
            terminate_and_reap(process)
        _join_reader(process.stdout if process is not None else None, stdout_thread)
        _join_reader(process.stderr if process is not None else None, stderr_thread)
    return BoundedProcessResult(
        returncode=returncode,
        stdout=stdout_capture.payload(),
        stderr=stderr_capture.payload(),
        stdout_truncated=stdout_capture.truncated,
        stderr_truncated=stderr_capture.truncated,
    )


def start_diagnostic_reader(
    process: subprocess.Popen[bytes],
    *,
    name: str,
) -> tuple[BoundedDiagnostic, threading.Thread]:
    """Start draining a process stderr pipe without permitting backpressure."""

    if process.stderr is None:
        raise RuntimeError("The media subprocess did not expose a diagnostic pipe.")
    diagnostic = BoundedDiagnostic(
        overflow_callback=lambda: terminate_and_reap(process)
    )
    return diagnostic, _start_reader(diagnostic, process.stderr, name=name)


def join_diagnostic_reader(
    process: subprocess.Popen[bytes] | None,
    thread: threading.Thread | None,
) -> None:
    _join_reader(process.stderr if process is not None else None, thread)


def _signal_process_scope(process: subprocess.Popen[bytes], *, force: bool) -> None:
    if os.name != "nt" and isinstance(process, _DeadlineProcess):
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except OSError:
            pass
        return
    try:
        if force:
            process.kill()
        else:
            process.terminate()
    except OSError:
        pass


def terminate_and_reap(
    process: subprocess.Popen[bytes] | None,
    *,
    timeout: float = 2.0,
) -> None:
    """Stop an owned process scope, escalate if needed, and reap its leader."""

    if process is None:
        return
    try:
        running = process.poll() is None
    except OSError:
        running = False
    if running:
        _signal_process_scope(process, force=False)
        try:
            process.wait(timeout=timeout)
            return
        except (OSError, subprocess.TimeoutExpired):
            _signal_process_scope(process, force=True)
    try:
        process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if isinstance(process, _DeadlineProcess):
        process._cancel_media_deadline()


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
    "MEDIA_ANALYSIS_TIMEOUT_SECONDS",
    "MEDIA_EVIDENCE_TIMEOUT_SECONDS",
    "MEDIA_QUERY_TIMEOUT_SECONDS",
    "MEDIA_RENDER_TIMEOUT_SECONDS",
    "join_diagnostic_reader",
    "media_process_timed_out",
    "require_ffmpeg_nostdin",
    "run_bounded_capture",
    "start_diagnostic_reader",
    "start_media_process",
    "terminate_and_reap",
]
