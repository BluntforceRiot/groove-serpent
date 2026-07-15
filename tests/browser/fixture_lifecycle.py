from __future__ import annotations

import ctypes
import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import BinaryIO, NamedTuple, Protocol


class StoppableServer(Protocol):
    def shutdown(self) -> None: ...


class ProcessIdentity(NamedTuple):
    pid: int
    creation_marker: int | None


OwnerProbe = Callable[[int], bool]


class _FileTime(ctypes.Structure):
    _fields_ = (("low", ctypes.c_uint32), ("high", ctypes.c_uint32))


class _IoCounters(ctypes.Structure):
    _fields_ = (
        ("read_operations", ctypes.c_uint64),
        ("write_operations", ctypes.c_uint64),
        ("other_operations", ctypes.c_uint64),
        ("read_bytes", ctypes.c_uint64),
        ("write_bytes", ctypes.c_uint64),
        ("other_bytes", ctypes.c_uint64),
    )


class _JobBasicLimitInformation(ctypes.Structure):
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


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = (
        ("basic", _JobBasicLimitInformation),
        ("io", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    )


_WINDOWS_JOB_HANDLE: int | None = None


def _install_owned_process_scope() -> None:
    """Put every fixture descendant in a kernel-owned lifetime boundary."""

    global _WINDOWS_JOB_HANDLE
    if os.name != "nt":
        try:
            os.setsid()
        except PermissionError:
            if os.getpgrp() != os.getpid():
                raise
        return

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

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise RuntimeError("Could not create the fixture process job.")
    limits = _JobExtendedLimitInformation()
    limits.basic.limit_flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle,
        9,  # JobObjectExtendedLimitInformation
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        kernel32.CloseHandle(handle)
        raise RuntimeError("Could not configure the fixture process job.")
    if not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise RuntimeError(f"Could not own the fixture process tree (Windows error {error}).")
    _WINDOWS_JOB_HANDLE = int(handle)


def _windows_process_identity(pid: int) -> ProcessIdentity | None:
    synchronize = 0x00100000
    query_limited_information = 0x00001000
    wait_timeout = 0x00000102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.GetProcessTimes.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    )
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(
        synchronize | query_limited_information,
        False,
        pid,
    )
    if not handle:
        return None
    try:
        if kernel32.WaitForSingleObject(handle, 0) != wait_timeout:
            return None
        created = _FileTime()
        exited = _FileTime()
        kernel = _FileTime()
        user = _FileTime()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        marker = (int(created.high) << 32) | int(created.low)
        return ProcessIdentity(pid, marker)
    finally:
        kernel32.CloseHandle(handle)


def _posix_creation_marker(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            raw = handle.read()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    fields = raw[raw.rfind(")") + 2 :].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def capture_process_identity(pid: int) -> ProcessIdentity | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_process_identity(pid)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return None
    return ProcessIdentity(pid, _posix_creation_marker(pid))


def process_identity_is_alive(identity: ProcessIdentity) -> bool:
    current = capture_process_identity(identity.pid)
    if current is None:
        return False
    if identity.creation_marker is None:
        return True
    return current.creation_marker == identity.creation_marker


def pid_is_alive(pid: int) -> bool:
    return capture_process_identity(pid) is not None


def _finite_lifetime(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    if not math.isfinite(parsed) or not 1.0 <= parsed <= 3_600.0:
        return default
    return parsed


class FixtureLifecycle:
    def __init__(
        self,
        server: StoppableServer | None,
        *,
        owner_pid: int | None,
        max_seconds: float,
        stdin: BinaryIO,
        owner_probe: OwnerProbe = pid_is_alive,
        poll_seconds: float = 0.25,
        install_signals: bool = True,
        owned_process_group: int | None = None,
    ) -> None:
        self._server = server
        self._owner_pid = owner_pid
        self._max_seconds = max_seconds
        self._stdin = stdin
        self._owner_probe = owner_probe
        self._poll_seconds = poll_seconds
        self._install_signals = install_signals
        self._owned_process_group = owned_process_group
        self._shutdown_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._reap_lock = threading.Lock()
        self._active_process: subprocess.Popen[bytes] | None = None
        self._stopping = threading.Event()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def attach_server(self, server: StoppableServer) -> None:
        with self._shutdown_lock:
            self._server = server
            stopping = self._stopping.is_set()
        if stopping:
            threading.Thread(target=server.shutdown, daemon=True).start()

    def raise_if_stopping(self) -> None:
        if self._stopping.is_set():
            raise RuntimeError("Fixture controller exited during startup.")

    def request_shutdown(self, reason: str) -> None:
        with self._shutdown_lock:
            if self._stopping.is_set():
                return
            self._stopping.set()
            server = self._server
        try:
            print(f"Fixture shutdown requested: {reason}", file=sys.stderr, flush=True)
        except OSError:
            pass
        with self._process_lock:
            process = self._active_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if server is not None:
            server.shutdown()

    def _watch_stdin(self) -> None:
        try:
            self._stdin.read(1)
        except OSError:
            pass
        self.request_shutdown("controller input closed")

    def _watch_owner(self) -> None:
        deadline = time.monotonic() + self._max_seconds
        while not self._stopping.wait(self._poll_seconds):
            if self._owner_pid is not None and not self._owner_probe(self._owner_pid):
                self.request_shutdown("controller process exited")
                return
            if time.monotonic() >= deadline:
                self.request_shutdown("maximum fixture lifetime reached")
                return

    def _handle_signal(self, signum: int, _frame: object) -> None:
        threading.Thread(
            target=self.request_shutdown,
            args=(f"signal {signum}",),
            daemon=True,
        ).start()

    def start(self) -> None:
        if self._install_signals:
            signal.signal(signal.SIGINT, self._handle_signal)
            signal.signal(signal.SIGTERM, self._handle_signal)
        threading.Thread(target=self._watch_stdin, daemon=True).start()
        threading.Thread(target=self._watch_owner, daemon=True).start()

    def run_startup_command(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float = 30.0,
    ) -> subprocess.CompletedProcess[bytes]:
        self.raise_if_stopping()
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with self._process_lock:
            if self._active_process is not None:
                process.kill()
                process.wait()
                raise RuntimeError("Fixture startup attempted overlapping tool processes.")
            self._active_process = process
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.1)
                    if self._stopping.is_set():
                        raise RuntimeError("Fixture controller exited during tool startup.")
                    break
                except subprocess.TimeoutExpired:
                    if self._stopping.is_set() or time.monotonic() >= deadline:
                        try:
                            process.terminate()
                            stdout, stderr = process.communicate(timeout=2.0)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            stdout, stderr = process.communicate(timeout=2.0)
                        if self._stopping.is_set():
                            raise RuntimeError("Fixture controller exited during tool startup.")
                        raise RuntimeError("Fixture startup tool exceeded its time limit.")
        finally:
            with self._process_lock:
                if self._active_process is process:
                    self._active_process = None
        return subprocess.CompletedProcess(
            list(command),
            process.returncode,
            stdout[-65_536:],
            stderr[-65_536:],
        )

    @staticmethod
    def _reap_exited_children() -> None:
        if os.name == "nt":
            return
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except (ChildProcessError, OSError):
                return
            if pid == 0:
                return

    def _owned_group_members(self) -> set[int]:
        group = self._owned_process_group
        if os.name == "nt" or group is None:
            return set()
        candidates: set[int] = set()
        proc = Path("/proc")
        if proc.is_dir():
            try:
                candidates = {int(entry.name) for entry in os.scandir(proc) if entry.name.isdigit()}
            except OSError:
                candidates = set()
        else:
            ps = next(
                (path for path in ("/bin/ps", "/usr/bin/ps") if Path(path).is_file()),
                None,
            )
            if ps is None:
                raise RuntimeError("Cannot enumerate the fixture process group.")
            completed = subprocess.run(
                [ps, "-axo", "pid="],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=5.0,
                text=True,
            )
            if completed.returncode != 0:
                raise RuntimeError("Cannot enumerate the fixture process group.")
            candidates = {int(value) for value in completed.stdout.split() if value.isdigit()}
        members: set[int] = set()
        for pid in candidates:
            if pid == os.getpid():
                continue
            try:
                if os.getpgid(pid) == group:
                    members.add(pid)
            except (ProcessLookupError, PermissionError):
                continue
        return members

    def _wait_for_owned_group_empty(self, timeout_seconds: float) -> set[int]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            self._reap_exited_children()
            members = self._owned_group_members()
            if not members or time.monotonic() >= deadline:
                return members
            time.sleep(0.02)

    def reap_owned_descendants(self) -> None:
        """Synchronously empty the fixture's POSIX group before its leader exits."""

        if os.name == "nt" or self._owned_process_group is None:
            return
        with self._reap_lock:
            members = self._owned_group_members()
            for signum, wait_seconds in (
                (signal.SIGTERM, 1.0),
                (signal.SIGKILL, 2.0),
            ):
                for pid in members:
                    try:
                        if os.getpgid(pid) == self._owned_process_group:
                            os.kill(pid, signum)
                    except (ProcessLookupError, PermissionError):
                        continue
                members = self._wait_for_owned_group_empty(wait_seconds)
                if not members:
                    return
            raise RuntimeError(
                "Fixture descendants survived forced cleanup: "
                + ", ".join(str(pid) for pid in sorted(members))
            )


def install_fixture_lifecycle(
    server: StoppableServer | None = None,
) -> FixtureLifecycle:
    _install_owned_process_scope()
    raw_owner = os.environ.get("GROOVE_SERPENT_FIXTURE_OWNER_PID")
    try:
        owner_pid = int(raw_owner) if raw_owner else None
    except ValueError:
        owner_pid = -1
    owner_identity = capture_process_identity(owner_pid) if owner_pid is not None else None

    def owner_probe(_pid: int) -> bool:
        return owner_identity is not None and process_identity_is_alive(owner_identity)

    max_seconds = _finite_lifetime(
        os.environ.get("GROOVE_SERPENT_FIXTURE_MAX_SECONDS"),
        180.0,
    )
    lifecycle = FixtureLifecycle(
        server,
        owner_pid=owner_pid,
        max_seconds=max_seconds,
        stdin=sys.stdin.buffer,
        owner_probe=owner_probe,
        owned_process_group=os.getpgrp() if os.name != "nt" else None,
    )
    lifecycle.start()
    return lifecycle
