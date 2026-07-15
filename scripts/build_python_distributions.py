"""Build reproducible, audited Python release distributions.

The build backend runs only against a fresh package-input snapshot.  The wheel
is retained after a strict RECORD audit; the sdist is rewritten as canonical
USTAR inside a deterministic RFC 1952 stream whose DEFLATE payload uses stored
blocks.  Stored blocks trade compression for byte-for-byte portability across
zlib implementations.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import csv
import hashlib
import io
import json
import os
import re
import secrets
import signal
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import zipfile
import zlib
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Sequence, cast


# The builder imports one project helper below.  Refuse interpreter bytecode
# side effects so invoking this script cannot mutate a clean source checkout.
sys.dont_write_bytecode = True


ROOT = Path(__file__).resolve().parent.parent
if not __package__:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from groove_serpent.executable_discovery import find_executable  # noqa: E402
from scripts._release_fs import (  # noqa: E402
    PathIdentity,
    canonical_portable_relative_path,
    capture_identity,
    ensure_plain_directory_path,
    inspect_plain_directory,
    inspect_single_link_file,
    read_single_link_file,
    remove_owned_tree,
    remove_owned_tree_candidates,
    rename_no_replace,
    require_stable_creation_identity,
    walk_plain_tree,
)


SCHEMA = "groove-serpent/python-distribution-build-receipt/1"
SOURCE_DATE_EPOCH = 315532800  # 1980-01-01T00:00:00Z; also the ZIP lower bound.
UV_VERSION = "0.11.28"
DEFAULT_OUTPUT = ROOT / "dist"
CONSTRAINTS_PATH = ROOT / "packaging" / "python-build-constraints.txt"
PACKAGE_ROOT_FILES = ("pyproject.toml", "README.md", "LICENSE")
PACKAGE_SOURCE_TREE = Path("src") / "groove_serpent"
MAX_INPUT_FILE_BYTES = 16 * 1024 * 1024
MAX_INPUT_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_FILE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_BYTES = 192 * 1024 * 1024
MAX_UV_BYTES = 128 * 1024 * 1024
MAX_TOOL_OUTPUT_BYTES = 8 * 1024 * 1024
USTAR_FILE_MODE = 0o100644
RECEIPT_NAME = "PYTHON_DISTRIBUTIONS_RECEIPT.json"
WINDOWS_WRAPPER_BOOTSTRAP = (
    "import sys;"
    "sys.dont_write_bytecode=True;"
    "sys.path.insert(0,sys.argv.pop(1));"
    "from scripts.build_python_distributions import _windows_owned_tool_wrapper;"
    "raise SystemExit(_windows_owned_tool_wrapper(sys.argv[1:]))"
)

# SHA-256 of the extracted official uv 0.11.28 executable, derived from the
# corresponding hash-published GitHub release archives.  Exact descendant-tree
# containment is implemented on Windows and Linux, so other hosts are refused.
TRUSTED_UV_EXECUTABLES = {
    "1cb9cd0a1749debf6049d7d2bb933882cc52d81016326ee6d99a786d6c988b03":
        "x86_64-unknown-linux-gnu",
    "533fe4044bc50b05ac89f4d07925597fdb5285369724e8986ecab356818f09ee":
        "x86_64-pc-windows-msvc",
    "960b3d22f5782c6a3b281487eb46b3e78a53950a4b8a02caa5e4442761759c5c":
        "aarch64-pc-windows-msvc",
    "b9f74e398b6b15826a4b68b5a83d039036d47df64013e7faf1a9974ec199c144":
        "aarch64-unknown-linux-gnu",
}
SETUPTOOLS_VERSION = "83.0.0"
SETUPTOOLS_HASHES = {
    "025bccbbf0fa05b6192bc64ae1e7b16e001fd6d6d4d5de03c97b1c1ade523bef",
    "29b23c360f22f414dc7336bb39178cc7bcbf6021ed2733cde173f09dba19abb3",
}
FORBIDDEN_SUFFIXES = {
    ".aif",
    ".aiff",
    ".flac",
    ".m4a",
    ".mp3",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".pyo",
    ".wav",
}
FORBIDDEN_ENDINGS = {
    ".album.json",
    ".click-scan.json",
    ".groove.json",
    ".restoration-recipe.json",
    ".tracklist.json",
}
PRIVATE_PATTERNS = (
    re.compile(rb"[A-Za-z]:[\\/]Users[\\/][^\\/\r\n]+", re.IGNORECASE),
    re.compile(rb"[A-Za-z]:[\\/]HomelabForge[\\/]", re.IGNORECASE),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"sk-" rb"proj-[A-Za-z0-9_-]+"),
)


@dataclass(frozen=True, slots=True)
class FileRecord:
    relative: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class WheelAudit:
    member_count: int
    payload_bytes: int
    metadata_sha256: str
    record_sha256: str


@dataclass(frozen=True, slots=True)
class SdistAudit:
    records: tuple[FileRecord, ...]
    raw_member_count: int
    payload_bytes: int


def _linux_direct_children(parent_pid: int) -> set[tuple[int, str]]:
    """Return direct-child PID/start-time identities from the Linux procfs."""

    proc = Path("/proc")
    if not proc.is_dir():
        raise RuntimeError("Linux descendant containment requires a mounted /proc.")
    children: set[tuple[int, str]] = set()
    try:
        entries = tuple(proc.iterdir())
    except OSError as exc:
        raise RuntimeError("Linux descendant containment cannot enumerate /proc.") from exc
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            value = (entry / "stat").read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            continue
        closing = value.rfind(")")
        fields = value[closing + 2 :].split() if closing >= 0 else []
        if len(fields) < 20:
            continue
        try:
            pid = int(entry.name)
            ppid = int(fields[1])
        except ValueError:
            continue
        if ppid == parent_pid:
            children.add((pid, fields[19]))
    return children


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


class _IoCounters(ctypes.Structure):
    _fields_ = (
        ("read_operations", ctypes.c_uint64),
        ("write_operations", ctypes.c_uint64),
        ("other_operations", ctypes.c_uint64),
        ("read_bytes", ctypes.c_uint64),
        ("write_bytes", ctypes.c_uint64),
        ("other_bytes", ctypes.c_uint64),
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


class _OwnedProcessScope:
    """Own one uv process tree and forcibly reap every descendant."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self._linux_previous_subreaper: int | None = None
        self._linux_baseline_children: set[tuple[int, str]] = set()

    def _prepare_linux_subreaper(self) -> None:
        if sys.platform != "linux":
            raise RuntimeError(
                "Exact distribution-build process containment supports Windows and Linux only."
            )
        libc: Any = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise RuntimeError(f"Could not inspect Linux subreaper state (errno {error}).")
        if previous.value not in {0, 1}:
            raise RuntimeError("Linux returned an invalid subreaper state.")
        if previous.value == 0 and libc.prctl(36, 1, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise RuntimeError(f"Could not enable Linux descendant ownership (errno {error}).")
        try:
            baseline = _linux_direct_children(os.getpid())
        except BaseException:
            if previous.value == 0:
                libc.prctl(36, 0, 0, 0, 0)
            raise
        self._linux_previous_subreaper = previous.value
        self._linux_baseline_children = baseline

    def _restore_linux_subreaper(self) -> None:
        previous = self._linux_previous_subreaper
        if previous is None:
            return
        libc: Any = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, previous, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise RuntimeError(f"Could not restore Linux subreaper state (errno {error}).")
        self._linux_previous_subreaper = None
        self._linux_baseline_children = set()

    def _reap_linux_descendants(self) -> None:
        if self._linux_previous_subreaper is None:
            return
        deadline = time.monotonic() + 5.0
        nohang = int(getattr(os, "WNOHANG", 1))
        while True:
            adopted = _linux_direct_children(os.getpid()) - self._linux_baseline_children
            if not adopted:
                self._restore_linux_subreaper()
                return
            pending: set[int] = set()
            for pid, _start_time in adopted:
                try:
                    os.kill(pid, getattr(signal, "SIGKILL", 9))
                except ProcessLookupError:
                    pass
                pending.add(pid)
            while pending:
                for pid in tuple(pending):
                    try:
                        waited, _status = os.waitpid(pid, nohang)
                    except (ChildProcessError, ProcessLookupError):
                        pending.remove(pid)
                        continue
                    if waited == pid:
                        pending.remove(pid)
                if pending and time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Linux distribution-build descendants survived forced cleanup."
                    )
                if pending:
                    time.sleep(0.02)

    def start(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        stdout: int | BinaryIO,
        stderr: int | BinaryIO,
    ) -> subprocess.Popen[bytes]:
        launched_command = list(command)
        if os.name == "nt":
            # The wrapper assigns itself to KILL_ON_JOB_CLOSE before it is
            # allowed to spawn uv.  Killing the wrapper therefore closes the
            # job handle and reaps the complete descendant tree without the
            # post-Popen assignment race.
            launched_command = [
                sys.executable,
                "-I",
                "-c",
                WINDOWS_WRAPPER_BOOTSTRAP,
                str(ROOT),
                *launched_command,
            ]
        else:
            self._prepare_linux_subreaper()
        process = subprocess.Popen(
            launched_command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=os.name != "nt",
        )
        self.process = process
        return process

    def close(self) -> None:
        process = self.process
        if os.name != "nt" and process is not None:
            try:
                kill_process_group: Any = getattr(os, "killpg")
                kill_process_group(process.pid, getattr(signal, "SIGKILL", 9))
            except ProcessLookupError:
                pass
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=5)
        if os.name != "nt":
            self._reap_linux_descendants()
        if os.name != "nt" and process is not None:
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    probe_process_group: Any = getattr(os, "killpg")
                    probe_process_group(process.pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Distribution-build process group survived forced cleanup.")
                time.sleep(0.02)


def _windows_owned_tool_wrapper(command: Sequence[str]) -> int:
    """Install the current process in a job before spawning one tool tree."""

    if os.name != "nt" or not command:
        raise RuntimeError("The owned-tool wrapper is Windows-only and requires a command.")
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
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
        raise RuntimeError("Could not create the distribution-build process job.")
    limits = _JobExtendedLimitInformation()
    limits.basic.limit_flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle,
        9,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        kernel32.CloseHandle(handle)
        raise RuntimeError("Could not configure the distribution-build process job.")
    if not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise RuntimeError(
            f"Could not pre-own the distribution-build process tree (Windows error {error})."
        )
    # The handle deliberately remains open for this wrapper's lifetime.  It is
    # non-inheritable, so normal exit or forced termination closes the last
    # handle and the kernel kills every still-running descendant.
    child = subprocess.Popen(list(command), stdin=subprocess.DEVNULL)
    return child.wait()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_sha256(value: str, context: str) -> str:
    normalized = value.casefold()
    if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
        raise RuntimeError(f"{context} is not one canonical SHA-256 digest.")
    return normalized


def _safe_relative(value: str, context: str) -> tuple[str, str]:
    canonical, portable = canonical_portable_relative_path(value, context)
    name = PurePosixPath(canonical).name.casefold()
    if (
        PurePosixPath(canonical).suffix.casefold() in FORBIDDEN_SUFFIXES
        or name.startswith(".env")
        or any(name.endswith(ending) for ending in FORBIDDEN_ENDINGS)
    ):
        raise RuntimeError(f"{context} is forbidden release material: {canonical}")
    return canonical, portable


def _audit_payload(relative: str, payload: bytes, context: str) -> None:
    encoded = relative.encode("utf-8")
    for pattern in PRIVATE_PATTERNS:
        if pattern.search(encoded) is not None or pattern.search(payload) is not None:
            raise RuntimeError(f"{context} contains private material: {relative}")


def _constraints(payload: bytes) -> dict[str, object]:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Python build constraints are not UTF-8.") from exc
    logical = re.sub(r"\\\r?\n\s*", " ", text)
    logical = "\n".join(
        line for line in logical.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )
    requirement = re.fullmatch(
        r"setuptools==(?P<version>[0-9]+(?:\.[0-9]+){2})"
        r"(?P<hashes>(?:\s+--hash=sha256:[0-9a-f]{64})+)",
        logical.strip(),
    )
    if requirement is None or requirement.group("version") != SETUPTOOLS_VERSION:
        raise RuntimeError("Python build constraints must pin exactly setuptools 83.0.0.")
    hashes = {
        token.removeprefix("--hash=sha256:")
        for token in requirement.group("hashes").split()
    }
    if hashes != SETUPTOOLS_HASHES:
        raise RuntimeError("The setuptools pin must carry both expected distribution hashes.")
    return {
        "file_sha256": _sha256(payload),
        "requirement": f"setuptools=={SETUPTOOLS_VERSION}",
        "distribution_hashes": sorted(hashes),
    }


def _package_metadata(root: Path) -> tuple[str, str, str, str]:
    payload = read_single_link_file(
        root / "pyproject.toml",
        MAX_INPUT_FILE_BYTES,
        "Python package metadata",
    )
    document = tomllib.loads(payload.decode("utf-8", errors="strict"))
    project = document.get("project")
    build = document.get("build-system")
    if not isinstance(project, dict) or not isinstance(build, dict):
        raise RuntimeError("pyproject.toml is missing project or build-system metadata.")
    name = project.get("name")
    version = project.get("version")
    backend = build.get("build-backend")
    requirements = build.get("requires")
    if (
        name != "groove-serpent"
        or not isinstance(version, str)
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", version) is None
        or set(build) != {"build-backend", "requires"}
        or backend != "setuptools.build_meta"
        or requirements != ["setuptools>=77"]
    ):
        raise RuntimeError(
            "Python package authority is not the exact external setuptools project."
        )
    normalized = re.sub(r"[-_.]+", "_", name)
    return name, version, normalized, f"{normalized}-{version}"


def _package_records(root: Path) -> tuple[FileRecord, ...]:
    root = Path(os.path.abspath(os.fspath(root)))
    records: list[FileRecord] = []
    portable_names: dict[str, str] = {}
    total = 0
    paths = [root / relative for relative in PACKAGE_ROOT_FILES]
    source = root / PACKAGE_SOURCE_TREE

    def skip_generated(path: Path) -> bool:
        relative = path.relative_to(source)
        return any(
            part.casefold() == "__pycache__" or part.casefold().endswith(".egg-info")
            for part in relative.parts
        )

    paths.extend(
        walk_plain_tree(source, "Python package source", skip_directory=skip_generated)
    )
    for path in paths:
        relative_path = path.relative_to(root)
        if any(
            part.casefold() == "__pycache__" or part.casefold().endswith(".egg-info")
            for part in relative_path.parts
        ):
            continue
        relative, portable = _safe_relative(
            relative_path.as_posix(),
            "Python package input",
        )
        previous = portable_names.get(portable)
        if previous is not None:
            raise RuntimeError(f"Portable package-input collision: {previous} and {relative}")
        payload = read_single_link_file(path, MAX_INPUT_FILE_BYTES, "Python package input")
        total += len(payload)
        if total > MAX_INPUT_TOTAL_BYTES:
            raise RuntimeError("Python package inputs exceed their aggregate byte ceiling.")
        _audit_payload(relative, payload, "Python package input")
        portable_names[portable] = relative
        records.append(FileRecord(relative, payload))
    return tuple(sorted(records, key=lambda item: item.relative.casefold()))


def _manifest_digest(records: Sequence[FileRecord]) -> str:
    manifest = "".join(
        f"{_sha256(record.payload)} {len(record.payload)} {record.relative}\n"
        for record in records
    ).encode("utf-8")
    return _sha256(manifest)


def _write_exact(path: Path, payload: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())
    os.chmod(path, mode)
    _set_epoch(path)
    if read_single_link_file(path, max(len(payload), 1), "Generated release file") != payload:
        raise RuntimeError(f"Generated release file changed after creation: {path.name}")


def _set_epoch(path: Path) -> None:
    """Set owned snapshot metadata without accepting a link substitution."""

    value = path.lstat()
    if stat.S_ISLNK(value.st_mode):
        raise RuntimeError(f"Owned build path changed into a link: {path}")
    try:
        os.utime(path, (SOURCE_DATE_EPOCH, SOURCE_DATE_EPOCH), follow_symlinks=False)
    except NotImplementedError:
        # CPython on Windows does not expose follow_symlinks=False for utime.
        # The path was created inside an unpredictable owned staging tree and
        # was inspected immediately above; re-inspect after the fallback.
        os.utime(path, (SOURCE_DATE_EPOCH, SOURCE_DATE_EPOCH))
        after = path.lstat()
        if stat.S_ISLNK(after.st_mode) or not os.path.samestat(value, after):
            raise RuntimeError(f"Owned build path changed while setting metadata: {path}")


def _copy_snapshot(records: Sequence[FileRecord], destination: Path) -> None:
    destination.mkdir()
    for record in records:
        _write_exact(destination / Path(*PurePosixPath(record.relative).parts), record.payload)
    for directory in sorted(
        (path for path in destination.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        _set_epoch(directory)
    _set_epoch(destination)


def _assert_source_unchanged(
    root: Path,
    expected: Sequence[FileRecord],
    expected_constraints: bytes,
) -> None:
    observed = _package_records(root)
    constraints = read_single_link_file(
        root / "packaging" / "python-build-constraints.txt",
        MAX_INPUT_FILE_BYTES,
        "Python build constraints",
    )
    if observed != tuple(expected) or constraints != expected_constraints:
        raise RuntimeError("Authoritative package inputs changed during the distribution build.")


def _copy_trusted_uv(source: Path, destination: Path) -> tuple[str, str]:
    before = inspect_single_link_file(source, "Trusted uv executable")
    if int(before.st_size) > MAX_UV_BYTES:
        raise RuntimeError("Trusted uv executable exceeds its byte ceiling.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    with destination.open("xb") as target:
        descriptor = os.open(source, flags)
        try:
            opened = os.fstat(descriptor)
            if not os.path.samestat(before, opened):
                raise RuntimeError("Trusted uv executable changed while opening.")
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                target.write(chunk)
        finally:
            os.close(descriptor)
        target.flush()
        os.fsync(target.fileno())
    after = inspect_single_link_file(source, "Trusted uv executable")
    if not os.path.samestat(before, after):
        raise RuntimeError("Trusted uv executable changed during snapshotting.")
    observed = digest.hexdigest()
    target_name = TRUSTED_UV_EXECUTABLES.get(observed)
    if target_name is None:
        raise RuntimeError(
            "uv is not an allowlisted official 0.11.28 executable; exact release build refused."
        )
    os.chmod(destination, 0o755)
    copied = read_single_link_file(destination, MAX_UV_BYTES, "Snapshotted uv executable")
    if _sha256(copied) != observed:
        raise RuntimeError("Snapshotted uv executable changed after creation.")
    return observed, target_name


def _build_environment(temporary: Path) -> dict[str, str]:
    blocked_prefixes = ("GIT_", "PIP_", "PYTHON", "SETUPTOOLS_", "UV_")
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(blocked_prefixes)
    }
    environment.update(
        {
            "HOME": str(temporary),
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "SOURCE_DATE_EPOCH": str(SOURCE_DATE_EPOCH),
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "TMPDIR": str(temporary),
            "TZ": "UTC",
            "UV_NO_CONFIG": "1",
            "UV_NO_PROGRESS": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }
    )
    return environment


def _run_bounded(
    command: Sequence[str],
    cwd: Path,
    environment: dict[str, str],
    *,
    timeout_seconds: float = 600.0,
) -> bytes:
    scope = _OwnedProcessScope()
    stop_readers = threading.Event()
    output_exceeded = threading.Event()
    reader_errors: list[BaseException] = []
    reader_error_lock = threading.Lock()
    stdout_payload = bytearray()
    stderr_payload = bytearray()
    streams: list[BinaryIO] = []
    readers: list[threading.Thread] = []
    timed_out = False
    returncode: int | None = None

    def drain(stream: BinaryIO, destination: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                remaining = MAX_TOOL_OUTPUT_BYTES - len(destination)
                if remaining > 0:
                    destination.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    output_exceeded.set()
                    stop_readers.set()
        except BaseException as exc:
            with reader_error_lock:
                reader_errors.append(exc)
            stop_readers.set()

    try:
        process = scope.start(
            command,
            cwd=cwd,
            environment=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("Distribution build command did not expose diagnostic pipes.")
        streams = [cast(BinaryIO, process.stdout), cast(BinaryIO, process.stderr)]
        readers = [
            threading.Thread(
                target=drain,
                args=(streams[0], stdout_payload),
                name="distribution-build-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=(streams[1], stderr_payload),
                name="distribution-build-stderr",
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + timeout_seconds
        while True:
            if stop_readers.is_set():
                break
            returncode = process.poll()
            if returncode is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            stop_readers.wait(min(remaining, 0.05))
    finally:
        try:
            # Closing the job or killing the process group also removes any uv
            # or backend descendant that outlived the direct child.
            scope.close()
        finally:
            for reader in readers:
                reader.join(timeout=5)
            for stream in streams:
                try:
                    stream.close()
                except OSError:
                    pass
    if any(reader.is_alive() for reader in readers):
        raise RuntimeError("Distribution-build diagnostic reader survived cleanup.")
    if reader_errors:
        raise RuntimeError("Distribution-build diagnostics could not be captured safely.") from (
            reader_errors[0]
        )
    if output_exceeded.is_set():
        raise RuntimeError("Distribution build command exceeded its output byte ceiling.")
    if timed_out:
        raise RuntimeError(f"Distribution build command exceeded {timeout_seconds:g} seconds.")
    if returncode != 0:
        detail = bytes(stderr_payload[-8192:]).decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Distribution build command failed: {detail}")
    return bytes(stdout_payload)


def _verify_uv(uv: Path, cwd: Path, environment: dict[str, str]) -> str:
    output = _run_bounded((str(uv), "--version"), cwd, environment)
    text = output.decode("utf-8", errors="strict").strip()
    if re.fullmatch(rf"uv {re.escape(UV_VERSION)}(?: \([^\r\n]+\))?", text) is None:
        raise RuntimeError(f"Expected uv {UV_VERSION}, observed {text!r}.")
    return text


def _run_uv_build(
    uv: Path,
    source: Path,
    output: Path,
    constraints: Path,
    environment: dict[str, str],
) -> None:
    output.mkdir()
    command = (
        str(uv),
        "--no-config",
        "--color",
        "never",
        "--no-progress",
        "build",
        str(source),
        "--out-dir",
        str(output),
        "--build-constraints",
        str(constraints),
        "--require-hashes",
        "--no-cache",
        "--no-create-gitignore",
        "--no-python-downloads",
        "--no-sources",
        "--force-pep517",
        "--python",
        sys.executable,
    )
    _run_bounded(command, source.parent, environment)


def _run_uv_wheel_from_sdist(
    uv: Path,
    sdist: Path,
    output: Path,
    constraints: Path,
    environment: dict[str, str],
) -> None:
    output.mkdir()
    command = (
        str(uv),
        "--no-config",
        "--color",
        "never",
        "--no-progress",
        "build",
        "--wheel",
        str(sdist),
        "--out-dir",
        str(output),
        "--build-constraints",
        str(constraints),
        "--require-hashes",
        "--no-cache",
        "--no-create-gitignore",
        "--no-python-downloads",
        "--no-sources",
        "--force-pep517",
        "--python",
        sys.executable,
    )
    _run_bounded(command, sdist.parent, environment)


def _only_files(root: Path, expected: set[str], context: str) -> dict[str, Path]:
    inspect_plain_directory(root, context)
    observed: dict[str, Path] = {}
    for path in walk_plain_tree(root, context):
        relative, _portable = _safe_relative(path.relative_to(root).as_posix(), context)
        observed[relative] = path
    if set(observed) != expected:
        raise RuntimeError(f"{context} inventory mismatch: {sorted(observed)}")
    return observed


def _wheel_zip_layout_is_exact(
    stream: BinaryIO,
    size: int,
    infos: Sequence[zipfile.ZipInfo],
) -> bool:
    """Bind every wheel byte to one canonical local or central ZIP record."""

    if size < 22 or not infos:
        return False
    try:
        original_position = stream.tell()
    except (OSError, ValueError):
        return False

    def read_at(offset: int, count: int) -> bytes:
        if offset < 0 or count < 0 or offset + count > size:
            return b""
        stream.seek(offset)
        return stream.read(count)

    try:
        eocd_offset = size - 22
        eocd = read_at(eocd_offset, 22)
        if len(eocd) != 22:
            return False
        (
            signature,
            disk_number,
            central_disk,
            entries_on_disk,
            entries_total,
            central_size,
            central_offset,
            comment_size,
        ) = struct.unpack("<4s4H2LH", eocd)
        if (
            signature != b"PK\x05\x06"
            or disk_number != 0
            or central_disk != 0
            or entries_on_disk != len(infos)
            or entries_total != len(infos)
            or comment_size != 0
            or central_offset + central_size != eocd_offset
        ):
            return False

        expected_local_offset = 0
        central_cursor = central_offset
        declared_payload_total = 0
        for info in infos:
            if (
                info.header_offset != expected_local_offset
                or info.is_dir()
                or info.date_time != (1980, 1, 1, 0, 0, 0)
                or info.create_system not in {0, 3}
                or info.create_version != 20
                or info.extract_version != 20
                or info.reserved != 0
                or info.volume != 0
                or info.internal_attr != 0
                or info.extra != b""
                or info.comment != b""
                or info.flag_bits not in {0, 0x0800}
                or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or info.file_size > MAX_ARCHIVE_FILE_BYTES
                or declared_payload_total + info.file_size > MAX_ARCHIVE_TOTAL_BYTES
            ):
                return False
            declared_payload_total += info.file_size
            local = read_at(expected_local_offset, 30)
            if len(local) != 30:
                return False
            (
                local_signature,
                local_extract_version,
                flags,
                compression,
                modified_time,
                modified_date,
                crc,
                compressed_size,
                uncompressed_size,
                name_size,
                extra_size,
            ) = struct.unpack("<4s5H3L2H", local)
            try:
                encoded_name = info.filename.encode("ascii")
                expected_flags = 0
            except UnicodeEncodeError:
                encoded_name = info.filename.encode("utf-8")
                expected_flags = 0x0800
            name = read_at(expected_local_offset + 30, name_size)
            extra = read_at(expected_local_offset + 30 + name_size, extra_size)
            if (
                local_signature != b"PK\x03\x04"
                or local_extract_version != info.extract_version
                or flags != info.flag_bits
                or flags != expected_flags
                or flags & 0x0008
                or compression != info.compress_type
                or modified_time != 0
                or modified_date != 0x0021
                or crc != info.CRC
                or compressed_size != info.compress_size
                or uncompressed_size != info.file_size
                or name != encoded_name
                or extra != b""
            ):
                return False
            data_offset = expected_local_offset + 30 + name_size + extra_size
            raw_member = read_at(data_offset, compressed_size)
            if len(raw_member) != compressed_size:
                return False
            if compression == zipfile.ZIP_STORED:
                if compressed_size != uncompressed_size:
                    return False
                decoded = raw_member
            else:
                try:
                    decompressor = zlib.decompressobj(-15)
                    decoded = decompressor.decompress(raw_member, uncompressed_size + 1)
                except zlib.error:
                    return False
                if (
                    not decompressor.eof
                    or decompressor.unused_data
                    or decompressor.unconsumed_tail
                ):
                    return False
            if (
                len(decoded) != uncompressed_size
                or binascii.crc32(decoded) & 0xFFFFFFFF != crc
            ):
                return False
            expected_local_offset += 30 + name_size + extra_size + compressed_size

            central = read_at(central_cursor, 46)
            if len(central) != 46:
                return False
            (
                central_signature,
                version_made,
                central_extract_version,
                central_flags,
                central_compression,
                central_modified_time,
                central_modified_date,
                central_crc,
                central_compressed_size,
                central_uncompressed_size,
                central_name_size,
                central_extra_size,
                central_comment_size,
                disk_start,
                internal_attr,
                external_attr,
                local_header_offset,
            ) = struct.unpack("<4s6H3L5H2L", central)
            central_name = read_at(central_cursor + 46, central_name_size)
            central_extra = read_at(
                central_cursor + 46 + central_name_size,
                central_extra_size,
            )
            central_comment = read_at(
                central_cursor + 46 + central_name_size + central_extra_size,
                central_comment_size,
            )
            if (
                central_signature != b"PK\x01\x02"
                or version_made != info.create_version | (info.create_system << 8)
                or central_extract_version != info.extract_version
                or central_flags != info.flag_bits
                or central_compression != info.compress_type
                or central_modified_time != 0
                or central_modified_date != 0x0021
                or central_crc != info.CRC
                or central_compressed_size != info.compress_size
                or central_uncompressed_size != info.file_size
                or central_name != encoded_name
                or central_extra != info.extra
                or central_comment != info.comment
                or disk_start != info.volume
                or internal_attr != info.internal_attr
                or external_attr != info.external_attr
                or local_header_offset != info.header_offset
            ):
                return False
            central_cursor += (
                46 + central_name_size + central_extra_size + central_comment_size
            )
        return bool(
            expected_local_offset == central_offset
            and central_cursor == central_offset + central_size
        )
    except (OSError, UnicodeEncodeError, ValueError, struct.error):
        return False
    finally:
        try:
            stream.seek(original_position)
        except (OSError, ValueError):
            pass


def _wheel_audit(payload: bytes, filename: str, name: str, version: str) -> WheelAudit:
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise RuntimeError("Wheel exceeds its archive byte ceiling.")
    expected_filename = f"{re.sub(r'[-_.]+', '_', name)}-{version}-py3-none-any.whl"
    if filename != expected_filename:
        raise RuntimeError(f"Unexpected wheel filename: {filename}")
    payloads: dict[str, bytes] = {}
    portable_names: dict[str, str] = {}
    total = 0
    try:
        container = io.BytesIO(payload)
        with zipfile.ZipFile(container, "r") as archive:
            if archive.comment:
                raise RuntimeError("Wheel archive comments are forbidden.")
            infos = archive.infolist()
            declared_total = 0
            for info in infos:
                if info.file_size > MAX_ARCHIVE_FILE_BYTES:
                    raise RuntimeError(
                        f"Wheel member violates its byte ceiling: {info.filename}"
                    )
                declared_total += info.file_size
                if declared_total > MAX_ARCHIVE_TOTAL_BYTES:
                    raise RuntimeError("Wheel payloads exceed their aggregate byte ceiling.")
            if not _wheel_zip_layout_is_exact(container, len(payload), infos):
                raise RuntimeError("Wheel has prefixed, trailing, or noncanonical ZIP data.")
            for info in infos:
                relative, portable = _safe_relative(info.filename, "Wheel member")
                if info.is_dir() or info.flag_bits & 0x1:
                    raise RuntimeError(
                        f"Wheel contains a directory or encrypted member: {relative}"
                    )
                if (
                    info.date_time != (1980, 1, 1, 0, 0, 0)
                    or info.create_system not in {0, 3}
                    or info.extra
                    or info.comment
                ):
                    raise RuntimeError(f"Wheel member metadata is not deterministic: {relative}")
                mode = info.external_attr >> 16
                if mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                    raise RuntimeError(f"Wheel contains a linked or special member: {relative}")
                previous = portable_names.get(portable)
                if previous is not None:
                    raise RuntimeError(f"Portable wheel collision: {previous} and {relative}")
                if info.file_size > MAX_ARCHIVE_FILE_BYTES:
                    raise RuntimeError(f"Wheel member violates its byte ceiling: {relative}")
                if total + info.file_size > MAX_ARCHIVE_TOTAL_BYTES:
                    raise RuntimeError("Wheel payloads exceed their aggregate byte ceiling.")
                data = archive.read(info)
                if len(data) != info.file_size:
                    raise RuntimeError(f"Wheel member violates its byte ceiling: {relative}")
                total += len(data)
                _audit_payload(relative, data, "Wheel member")
                portable_names[portable] = relative
                payloads[relative] = data
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError("Wheel cannot be reopened safely.") from exc
    prefix = f"{re.sub(r'[-_.]+', '_', name)}-{version}.dist-info"
    metadata_name = f"{prefix}/METADATA"
    wheel_name = f"{prefix}/WHEEL"
    record_name = f"{prefix}/RECORD"
    required = {
        "groove_serpent/__init__.py",
        "groove_serpent/web/index.html",
        "groove_serpent/web/app.js",
        "groove_serpent/web/styles.css",
        metadata_name,
        wheel_name,
        record_name,
    }
    if not required.issubset(payloads):
        missing = sorted(required - payloads.keys())
        raise RuntimeError(f"Wheel is missing required members: {missing}")
    metadata_payload = payloads[metadata_name]
    metadata_payload.decode("utf-8", errors="strict")
    metadata = BytesParser().parsebytes(metadata_payload)
    if (
        metadata.defects
        or metadata.get_all("Name", []) != [name]
        or metadata.get_all("Version", []) != [version]
    ):
        raise RuntimeError(
            "Wheel METADATA must declare exactly one authoritative name and version."
        )
    wheel_payload = payloads[wheel_name]
    wheel_payload.decode("utf-8", errors="strict")
    wheel_metadata = BytesParser().parsebytes(wheel_payload)
    if (
        wheel_metadata.defects
        or wheel_metadata.get_payload() != ""
        or wheel_metadata.get_all("Wheel-Version", []) != ["1.0"]
        or wheel_metadata.get_all("Root-Is-Purelib", []) != ["true"]
        or wheel_metadata.get_all("Tag", []) != ["py3-none-any"]
    ):
        raise RuntimeError(
            "Wheel compatibility metadata must declare exactly one pure-Python tag."
        )
    try:
        rows = list(csv.reader(io.StringIO(payloads[record_name].decode("utf-8", "strict"))))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise RuntimeError("Wheel RECORD is malformed.") from exc
    recorded: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3 or row[0] in recorded:
            raise RuntimeError("Wheel RECORD has a duplicate or malformed row.")
        recorded[row[0]] = (row[1], row[2])
    if set(recorded) != set(payloads):
        raise RuntimeError("Wheel RECORD inventory does not match archive members.")
    for relative, data in payloads.items():
        digest, size = recorded[relative]
        if relative == record_name:
            if digest or size:
                raise RuntimeError("Wheel RECORD must leave its own hash and size empty.")
            continue
        encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        if digest != f"sha256={encoded}" or size != str(len(data)):
            raise RuntimeError(f"Wheel RECORD does not bind {relative}.")
    return WheelAudit(
        len(payloads),
        total,
        _sha256(payloads[metadata_name]),
        _sha256(payloads[record_name]),
    )


def _decode_exact_sdist_gzip(payload: bytes) -> bytes:
    """Decode one bounded gzip member and reject every unconsumed input byte."""

    try:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = decompressor.decompress(payload, MAX_ARCHIVE_BYTES + 1)
    except zlib.error as exc:
        raise RuntimeError("Source distribution gzip stream is malformed.") from exc
    if (
        len(decoded) > MAX_ARCHIVE_BYTES
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise RuntimeError("Source distribution must contain one exact bounded gzip stream.")
    return decoded


def _sdist_records(payload: bytes, root_name: str) -> SdistAudit:
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise RuntimeError("Source distribution exceeds its archive byte ceiling.")
    tar_payload = _decode_exact_sdist_gzip(payload)
    records: list[FileRecord] = []
    portable_names: dict[str, str] = {}
    entry_types: dict[str, str] = {}
    total = 0
    raw_members = 0
    tar_end = -1
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_payload), mode="r:") as archive:
            for member in archive:
                raw_members += 1
                relative, portable = _safe_relative(member.name, "Source distribution member")
                path = PurePosixPath(relative)
                if path.parts[0] != root_name or len(path.parts) == 1:
                    if len(path.parts) == 1 and member.isdir() and path.name == root_name:
                        pass
                    else:
                        raise RuntimeError("Source distribution escaped its one canonical root.")
                previous = portable_names.get(portable)
                if previous is not None:
                    raise RuntimeError(f"Portable sdist collision: {previous} and {relative}")
                portable_names[portable] = relative
                if member.isdir():
                    entry_types[relative] = "directory"
                    continue
                if not member.isfile() or member.islnk() or member.issym():
                    raise RuntimeError(
                        f"Source distribution contains a link or special: {relative}"
                    )
                handle = archive.extractfile(member)
                if handle is None:
                    raise RuntimeError(f"Source distribution member cannot be read: {relative}")
                data = handle.read(MAX_ARCHIVE_FILE_BYTES + 1)
                if len(data) != member.size or len(data) > MAX_ARCHIVE_FILE_BYTES:
                    raise RuntimeError(
                        f"Source distribution member violates its ceiling: {relative}"
                    )
                total += len(data)
                if total > MAX_ARCHIVE_TOTAL_BYTES:
                    raise RuntimeError("Source distribution exceeds its aggregate byte ceiling.")
                _audit_payload(relative, data, "Source distribution member")
                entry_types[relative] = "file"
                records.append(FileRecord(relative, data))
            tar_end = archive.offset
    except (OSError, tarfile.TarError) as exc:
        raise RuntimeError("Source distribution cannot be reopened safely.") from exc
    if (
        tar_end < 0
        or len(tar_payload) % tarfile.BLOCKSIZE != 0
        or len(tar_payload) - tar_end < 2 * tarfile.BLOCKSIZE
        or any(tar_payload[tar_end:])
    ):
        raise RuntimeError("Source distribution has an unbound or malformed tar ending.")
    if not records:
        raise RuntimeError("Source distribution contains no regular files.")
    for relative, kind in entry_types.items():
        parts = PurePosixPath(relative).parts
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if entry_types.get(parent) == "file":
                raise RuntimeError(f"Source distribution nests beneath a file: {relative}")
        if kind == "directory" and relative in {record.relative for record in records}:
            raise RuntimeError(f"Source distribution path changes type: {relative}")
    ordered = tuple(sorted(records, key=lambda item: item.relative.casefold()))
    return SdistAudit(ordered, raw_members, total)


def _canonical_tar(records: Sequence[FileRecord]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:", format=tarfile.USTAR_FORMAT) as archive:
        for record in records:
            info = tarfile.TarInfo(record.relative)
            info.size = len(record.payload)
            info.mtime = SOURCE_DATE_EPOCH
            info.mode = USTAR_FILE_MODE & 0o7777
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.type = tarfile.REGTYPE
            info.linkname = ""
            info.pax_headers = {}
            try:
                info.tobuf(format=tarfile.USTAR_FORMAT, encoding="utf-8", errors="strict")
            except (UnicodeError, ValueError) as exc:
                message = f"Source member cannot use canonical USTAR: {record.relative}"
                raise RuntimeError(message) from exc
            archive.addfile(info, io.BytesIO(record.payload))
    return stream.getvalue()


def _stored_gzip(payload: bytes) -> bytes:
    header = b"\x1f\x8b\x08\x00" + struct.pack("<I", SOURCE_DATE_EPOCH) + b"\x00\xff"
    blocks = bytearray()
    if not payload:
        blocks.extend(b"\x01\x00\x00\xff\xff")
    else:
        for offset in range(0, len(payload), 65535):
            chunk = payload[offset : offset + 65535]
            final = offset + len(chunk) == len(payload)
            blocks.append(1 if final else 0)
            blocks.extend(struct.pack("<HH", len(chunk), len(chunk) ^ 0xFFFF))
            blocks.extend(chunk)
    trailer = struct.pack("<II", binascii.crc32(payload) & 0xFFFFFFFF, len(payload) & 0xFFFFFFFF)
    return header + bytes(blocks) + trailer


def _decode_exact_stored_gzip(payload: bytes) -> bytes:
    expected_header = b"\x1f\x8b\x08\x00" + struct.pack("<I", SOURCE_DATE_EPOCH) + b"\x00\xff"
    if len(payload) < 23 or payload[:10] != expected_header:
        raise RuntimeError("Normalized sdist has noncanonical gzip metadata.")
    cursor = 10
    end = len(payload) - 8
    decoded = bytearray()
    final_seen = False
    while cursor < end:
        header = payload[cursor]
        cursor += 1
        if header not in {0, 1} or final_seen or cursor + 4 > end:
            raise RuntimeError("Normalized sdist has a noncanonical DEFLATE block.")
        length, inverse = struct.unpack_from("<HH", payload, cursor)
        cursor += 4
        if inverse != length ^ 0xFFFF or cursor + length > end:
            raise RuntimeError("Normalized sdist has a malformed stored DEFLATE block.")
        decoded.extend(payload[cursor : cursor + length])
        cursor += length
        final_seen = header == 1
    if cursor != end or not final_seen:
        raise RuntimeError("Normalized sdist has unbound or unterminated DEFLATE data.")
    crc, size = struct.unpack("<II", payload[-8:])
    result = bytes(decoded)
    if crc != binascii.crc32(result) & 0xFFFFFFFF or size != len(result) & 0xFFFFFFFF:
        raise RuntimeError("Normalized sdist gzip trailer is invalid.")
    return result


def _normalized_sdist(records: Sequence[FileRecord]) -> bytes:
    tar_payload = _canonical_tar(records)
    payload = _stored_gzip(tar_payload)
    reopened = _decode_exact_stored_gzip(payload)
    if reopened != tar_payload or _canonical_tar(records) != reopened:
        raise RuntimeError("Normalized sdist failed exact gzip/USTAR verification.")
    root_name = PurePosixPath(records[0].relative).parts[0]
    audited = _sdist_records(payload, root_name)
    if audited.records != tuple(records):
        raise RuntimeError("Normalized sdist changed its canonical file records.")
    return payload


def _create_owned_directory(parent: Path, prefix: str) -> tuple[Path, PathIdentity]:
    for _attempt in range(32):
        candidate = parent / f".{prefix}-{os.getpid()}-{secrets.token_hex(16)}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate, capture_identity(candidate)
    raise RuntimeError("Could not reserve a unique distribution staging directory.")


def _assert_directory_disjoint_from_package_source(
    root: Path,
    directory: Path,
    context: str,
) -> None:
    source = root / PACKAGE_SOURCE_TREE
    input_directories = [source]
    input_directories.extend(
        path
        for path in walk_plain_tree(
            source,
            "Python package output-overlap input",
            include_directories=True,
        )
        if path.is_dir()
    )
    current = directory
    while True:
        if os.path.lexists(current):
            for input_directory in input_directories:
                try:
                    if os.path.samefile(current, input_directory):
                        raise RuntimeError(f"{context} overlaps the package source tree.")
                except FileNotFoundError:
                    continue
        parent = current.parent
        if parent == current:
            break
        current = parent


def build_python_distributions(
    output: Path = DEFAULT_OUTPUT,
    *,
    root: Path = ROOT,
    uv_path: str | None = None,
    work_parent: Path | None = None,
) -> dict[str, object]:
    root = Path(os.path.abspath(os.fspath(root)))
    inspect_plain_directory(root, "Python distribution source root")
    name, version, normalized_name, distribution_root = _package_metadata(root)
    constraints_payload = read_single_link_file(
        root / "packaging" / "python-build-constraints.txt",
        MAX_INPUT_FILE_BYTES,
        "Python build constraints",
    )
    constraints_receipt = _constraints(constraints_payload)
    records = _package_records(root)
    source_manifest_sha256 = _manifest_digest(records)
    output = Path(os.path.abspath(os.fspath(output.expanduser())))
    _assert_directory_disjoint_from_package_source(
        root,
        output.parent,
        "Distribution output",
    )
    require_stable_creation_identity(output.parent, "Python distribution output")
    ensure_plain_directory_path(output.parent, "Python distribution output directory", create=True)
    if os.path.lexists(output):
        raise FileExistsError(f"Refusing to replace Python distribution output: {output}")
    selected_work_parent = (
        Path(tempfile.gettempdir()) if work_parent is None else work_parent.expanduser()
    )
    selected_work_parent = Path(os.path.abspath(os.fspath(selected_work_parent)))
    _assert_directory_disjoint_from_package_source(
        root,
        selected_work_parent,
        "Distribution work root",
    )
    require_stable_creation_identity(selected_work_parent, "Python distribution work root")
    ensure_plain_directory_path(
        selected_work_parent,
        "Python distribution work directory",
        create=True,
    )
    discovered = find_executable("uv", explicit=uv_path)
    if discovered is None:
        raise RuntimeError("uv must resolve from an absolute trusted PATH entry or explicit path.")

    work, work_identity = _create_owned_directory(
        selected_work_parent,
        "groove-serpent-python-distributions",
    )
    stage: Path | None = None
    stage_identity: PathIdentity | None = None
    work_cleaned = False
    rename_attempted = False
    rename_failed_known = False
    try:
        stage, stage_identity = _create_owned_directory(output.parent, output.name + ".stage")
        snapshot = work / "package-input"
        _copy_snapshot(records, snapshot)
        constraints = work / "python-build-constraints.txt"
        _write_exact(constraints, constraints_payload)
        uv_snapshot = work / ("uv.exe" if os.name == "nt" else "uv")
        uv_sha256, uv_target = _copy_trusted_uv(Path(discovered), uv_snapshot)
        temporary = work / "temporary"
        temporary.mkdir()
        environment = _build_environment(temporary)
        uv_version_output = _verify_uv(uv_snapshot, work, environment)
        _assert_source_unchanged(root, records, constraints_payload)

        raw = work / "raw"
        _run_uv_build(uv_snapshot, snapshot, raw, constraints, environment)
        wheel_name = f"{normalized_name}-{version}-py3-none-any.whl"
        sdist_name = f"{normalized_name}-{version}.tar.gz"
        built = _only_files(raw, {wheel_name, sdist_name}, "Raw Python distributions")
        wheel_payload = read_single_link_file(
            built[wheel_name], MAX_ARCHIVE_BYTES, "Raw Python wheel"
        )
        wheel_audit = _wheel_audit(wheel_payload, wheel_name, name, version)
        raw_sdist = read_single_link_file(
            built[sdist_name], MAX_ARCHIVE_BYTES, "Raw Python source distribution"
        )
        raw_sdist_audit = _sdist_records(raw_sdist, distribution_root)
        normalized_sdist = _normalized_sdist(raw_sdist_audit.records)
        normalized_path = work / sdist_name
        _write_exact(normalized_path, normalized_sdist)

        rebuilt = work / "rebuilt-wheel"
        _run_uv_wheel_from_sdist(
            uv_snapshot,
            normalized_path,
            rebuilt,
            constraints,
            environment,
        )
        rebuilt_files = _only_files(rebuilt, {wheel_name}, "Normalized sdist rebuild")
        rebuilt_wheel = read_single_link_file(
            rebuilt_files[wheel_name], MAX_ARCHIVE_BYTES, "Normalized sdist rebuilt wheel"
        )
        _wheel_audit(rebuilt_wheel, wheel_name, name, version)
        if rebuilt_wheel != wheel_payload:
            raise RuntimeError("Normalized sdist did not reproduce the exact audited wheel.")
        _assert_source_unchanged(root, records, constraints_payload)

        _write_exact(stage / wheel_name, wheel_payload)
        _write_exact(stage / sdist_name, normalized_sdist)
        receipt: dict[str, object] = {
            "schema": SCHEMA,
            "result": "passed",
            "project": {"name": name, "version": version},
            "source_date_epoch": SOURCE_DATE_EPOCH,
            "source_snapshot": {
                "file_count": len(records),
                "payload_bytes": sum(len(record.payload) for record in records),
                "manifest_sha256": source_manifest_sha256,
                "authoritative_inputs_unchanged": True,
            },
            "build_backend": constraints_receipt,
            "uv": {
                "version": UV_VERSION,
                "version_output": uv_version_output,
                "executable_sha256": uv_sha256,
                "official_release_target": uv_target,
            },
            "normalization": {
                "tar_format": "USTAR",
                "tar_mtime": SOURCE_DATE_EPOCH,
                "uid": 0,
                "gid": 0,
                "file_mode": "0644",
                "gzip_mtime": SOURCE_DATE_EPOCH,
                "gzip_os": 255,
                "deflate": "stored-blocks",
                "size_tradeoff": "portable deterministic bytes in exchange for no compression",
                "normalized_sdist_bytes": len(normalized_sdist),
                "exact_reopen_verified": True,
                "rebuilt_wheel_byte_identical": True,
            },
            "outputs": [
                {
                    "role": "wheel",
                    "filename": wheel_name,
                    "bytes": len(wheel_payload),
                    "sha256": _sha256(wheel_payload),
                    "member_count": wheel_audit.member_count,
                    "payload_bytes": wheel_audit.payload_bytes,
                    "metadata_sha256": wheel_audit.metadata_sha256,
                    "record_sha256": wheel_audit.record_sha256,
                },
                {
                    "role": "sdist",
                    "filename": sdist_name,
                    "bytes": len(normalized_sdist),
                    "sha256": _sha256(normalized_sdist),
                    "member_count": len(raw_sdist_audit.records),
                    "payload_bytes": raw_sdist_audit.payload_bytes,
                    "raw_member_count": raw_sdist_audit.raw_member_count,
                },
            ],
            "publication": {
                "staged": True,
                "directory_rename_no_replace": True,
                "destination_preexisting": False,
                "work_cleanup_completed_before_publication": True,
                "rename_is_terminal_fallible_visibility_operation": True,
                "postpublication_reopen_or_rehash_claimed": False,
            },
        }
        receipt_payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _write_exact(stage / RECEIPT_NAME, receipt_payload)
        expected = {wheel_name, sdist_name, RECEIPT_NAME}
        staged = _only_files(stage, expected, "Staged Python distributions")
        if (
            read_single_link_file(staged[wheel_name], MAX_ARCHIVE_BYTES, "Staged wheel")
            != wheel_payload
            or read_single_link_file(staged[sdist_name], MAX_ARCHIVE_BYTES, "Staged sdist")
            != normalized_sdist
            or read_single_link_file(staged[RECEIPT_NAME], MAX_INPUT_FILE_BYTES, "Staged receipt")
            != receipt_payload
        ):
            raise RuntimeError("Staged Python distributions changed before publication.")
        if not remove_owned_tree(work, work_identity):
            raise RuntimeError("Distribution work-directory cleanup lost ownership.")
        work_cleaned = True
        _assert_source_unchanged(root, records, constraints_payload)
        if capture_identity(stage) != stage_identity or os.path.lexists(output):
            raise RuntimeError("Python distribution publication boundary changed.")
        rename_attempted = True
        try:
            # Terminal fallible visibility operation: no filesystem, process,
            # cleanup, serialization, or verification work follows success.
            rename_no_replace(stage, output)
        except FileExistsError:
            rename_failed_known = True
            raise
        return receipt
    finally:
        cleanup_error: str | None = None
        if not work_cleaned and not remove_owned_tree(work, work_identity):
            cleanup_error = "Distribution work-directory cleanup lost ownership."
        if (
            (not rename_attempted or rename_failed_known)
            and stage is not None
            and stage_identity is not None
        ):
            if not remove_owned_tree_candidates((stage, output), stage_identity):
                cleanup_error = "Distribution staging cleanup lost ownership."
        if cleanup_error is not None:
            active = sys.exception()
            if active is None:
                raise RuntimeError(cleanup_error)
            active.add_note(cleanup_error)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build audited, reproducible Groove Serpent wheel and sdist artifacts."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--uv", help="Absolute uv 0.11.28 executable path; trusted PATH by default")
    parser.add_argument(
        "--work-parent",
        type=Path,
        help="Short plain-directory root for ephemeral build state; system temp by default",
    )
    args = parser.parse_args()
    receipt = build_python_distributions(
        args.output,
        uv_path=args.uv,
        work_parent=args.work_parent,
    )
    outputs = cast(list[dict[str, object]], receipt["outputs"])
    rendered: dict[str, str] = {}
    for item in outputs:
        role = item.get("role")
        digest = item.get("sha256")
        if not isinstance(role, str) or not isinstance(digest, str):
            raise RuntimeError("Distribution receipt output summary is malformed.")
        rendered[role] = digest
    print(json.dumps({"ok": True, "output": str(args.output), "sha256": rendered}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
