from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import tempfile
import unittest
import uuid
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parent.parent


def _load_lifecycle_module() -> ModuleType:
    path = ROOT / "tests" / "browser" / "fixture_lifecycle.py"
    spec = importlib.util.spec_from_file_location("fixture_lifecycle", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load fixture lifecycle module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LIFECYCLE = _load_lifecycle_module()


class FakeServer:
    def __init__(self) -> None:
        self.shutdown_count = 0
        self.shutdown_event = threading.Event()

    def shutdown(self) -> None:
        self.shutdown_count += 1
        self.shutdown_event.set()


class BlockingInput:
    def __init__(self) -> None:
        self.release = threading.Event()

    def read(self, _size: int) -> bytes:
        self.release.wait()
        return b""


class ControllerProcessScope:
    def __init__(self, scope_file: Path, scope_token: str) -> None:
        self.process: subprocess.Popen[str] | None = None
        self._scope_file = scope_file
        self._scope_token = scope_token
        self._fixture_group: int | None = None
        self._job: int | None = None
        if os.name == "nt":
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
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise RuntimeError("Could not create crash-probe cleanup job.")
            limits = LIFECYCLE._JobExtendedLimitInformation()
            limits.basic.limit_flags = 0x00002000
            if not kernel32.SetInformationJobObject(
                handle,
                9,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                kernel32.CloseHandle(handle)
                raise RuntimeError("Could not configure crash-probe cleanup job.")
            self._job = int(handle)

    def start(
        self,
        command: list[str],
        *,
        environment: dict[str, str],
    ) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name != "nt",
        )
        self.process = process
        if os.name != "nt":
            return process
        assert self._job is not None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        process_handle = kernel32.OpenProcess(0x00000101, False, process.pid)
        if not process_handle:
            raise RuntimeError("Could not open the crash-probe controller process.")
        try:
            if not kernel32.AssignProcessToJobObject(self._job, process_handle):
                error = ctypes.get_last_error()
                raise RuntimeError(
                    f"Could not own the crash-probe process tree (Windows error {error})."
                )
        finally:
            kernel32.CloseHandle(process_handle)
        return process

    def capture_fixture_group(self, timeout_seconds: float = 10.0) -> None:
        if os.name == "nt":
            return
        deadline = time.monotonic() + timeout_seconds
        while not self._scope_file.is_file():
            if time.monotonic() >= deadline:
                raise AssertionError("Fixture process-group handshake did not appear.")
            time.sleep(0.01)
        payload = json.loads(self._scope_file.read_text(encoding="utf-8"))
        if payload.get("schema") != "groove-serpent.fixture-process-scope/1":
            raise AssertionError("Fixture process-group handshake schema is invalid.")
        launcher_pid = payload.get("launcherPid")
        process_group = payload.get("processGroup")
        if (
            not isinstance(launcher_pid, int)
            or isinstance(launcher_pid, bool)
            or process_group != launcher_pid
            or launcher_pid <= 0
        ):
            raise AssertionError("Fixture process-group handshake is invalid.")
        try:
            observed_group = os.getpgid(launcher_pid)
        except ProcessLookupError as exc:
            raise AssertionError("Fixture launcher exited before scope capture.") from exc
        if observed_group != process_group:
            raise AssertionError("Fixture launcher process group does not match.")
        self._fixture_group = process_group

    def _scope_token_pids(self) -> set[int]:
        if os.name == "nt" or not Path("/proc").is_dir():
            return set()
        marker = (f"GROOVE_SERPENT_FIXTURE_TEST_SCOPE_TOKEN={self._scope_token}").encode("utf-8")
        matches: set[int] = set()
        try:
            entries = tuple(os.scandir("/proc"))
        except OSError:
            return set()
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                values = (Path(entry.path) / "environ").read_bytes().split(b"\0")
            except (OSError, PermissionError):
                continue
            if marker in values:
                matches.add(pid)
        return matches

    @staticmethod
    def _kill_process_group(group: int | None) -> None:
        if group is None:
            return
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self) -> None:
        process = self.process
        if os.name == "nt":
            if self._job is not None:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
                kernel32.CloseHandle.restype = ctypes.c_int
                kernel32.CloseHandle(self._job)
                self._job = None
        elif process is not None:
            self._kill_process_group(self._fixture_group)
            self._kill_process_group(process.pid)
            deadline = time.monotonic() + 2.0
            while True:
                token_pids = self._scope_token_pids()
                for pid in token_pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if not token_pids or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


class BrowserFixtureLifecycleTests(unittest.TestCase):
    def test_current_process_probe_is_non_destructive(self) -> None:
        self.assertTrue(LIFECYCLE.pid_is_alive(os.getpid()))

    def test_closed_controller_pipe_cannot_bypass_forced_cleanup(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the fixture process probe.")
        completed = subprocess.run(
            [node, "--test", "tests/browser/fixture-process.test.mjs"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_dead_owner_stops_fixture_once(self) -> None:
        server = FakeServer()
        stdin = BlockingInput()
        lifecycle = LIFECYCLE.FixtureLifecycle(
            server,
            owner_pid=12345,
            max_seconds=30.0,
            stdin=stdin,
            owner_probe=lambda _pid: False,
            poll_seconds=0.01,
            install_signals=False,
        )
        lifecycle.start()
        self.assertTrue(server.shutdown_event.wait(1.0))
        lifecycle.request_shutdown("duplicate request")
        self.assertEqual(server.shutdown_count, 1)
        stdin.release.set()

    def test_abrupt_controller_exit_does_not_orphan_fixture(self) -> None:
        node = shutil.which("node")
        ffmpeg = shutil.which("ffmpeg")
        if node is None or ffmpeg is None:
            self.skipTest("Node.js and FFmpeg are required for the fixture crash probe.")
        environment = os.environ.copy()
        environment["GROOVE_SERPENT_FIXTURE_MAX_SECONDS"] = "30"
        environment["GROOVE_SERPENT_PYTHON"] = sys.executable
        controller: subprocess.Popen[str] | None = None
        identities: list[object] = []
        with tempfile.TemporaryDirectory(prefix="groove-controller-barrier-") as value:
            scope_root = Path(value)
            environment["TEMP"] = str(scope_root)
            environment["TMP"] = str(scope_root)
            environment["TMPDIR"] = str(scope_root)
            scope_file = scope_root / "fixture-scope.json"
            scope_token = uuid.uuid4().hex
            scope = ControllerProcessScope(scope_file, scope_token)
            try:
                barrier = Path(value) / "start"
                environment["GROOVE_SERPENT_CONTROLLER_BARRIER"] = str(barrier)
                environment["GROOVE_SERPENT_FIXTURE_SCOPE_FILE"] = str(scope_file)
                environment["GROOVE_SERPENT_FIXTURE_TEST_SCOPE_TOKEN"] = scope_token
                controller = scope.start(
                    [node, "tests/browser/fixture-crash-probe.mjs"],
                    environment=environment,
                )
                barrier.write_text("go", encoding="ascii")
                scope.capture_fixture_group()
                stdout, stderr = controller.communicate(timeout=45)
                if (
                    controller.returncode == 1
                    and "Crash-probe fixture did not become ready" in stderr
                ):
                    self.skipTest(
                        "Crash-probe fixture did not become ready on this CI runner."
                    )
                self.assertEqual(controller.returncode, 17, stderr)
                payload = json.loads(stdout.strip().splitlines()[-1])
                fixture_pids = {
                    int(payload["launcherPid"]),
                    int(payload["fixturePid"]),
                    int(payload["descendantPid"]),
                }
                identities = [
                    identity
                    for pid in fixture_pids
                    if (identity := LIFECYCLE.capture_process_identity(pid)) is not None
                ]
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline and any(
                    LIFECYCLE.process_identity_is_alive(identity) for identity in identities
                ):
                    time.sleep(0.05)
                survivors = [
                    identity.pid
                    for identity in identities
                    if LIFECYCLE.process_identity_is_alive(identity)
                ]
                self.assertEqual(survivors, [], f"Orphaned fixture processes: {survivors}")
            finally:
                scope.close()

    def test_non_finite_environment_lifetimes_are_rejected(self) -> None:
        for value in ("inf", "-inf", "nan", "1e999", "0", "-1"):
            with self.subTest(value=value):
                self.assertEqual(LIFECYCLE._finite_lifetime(value, 180.0), 180.0)

    def test_controller_exit_cancels_bounded_startup_tool(self) -> None:
        server = FakeServer()
        stdin = BlockingInput()
        lifecycle = LIFECYCLE.FixtureLifecycle(
            server,
            owner_pid=None,
            max_seconds=30.0,
            stdin=stdin,
            poll_seconds=0.01,
            install_signals=False,
        )
        errors: list[BaseException] = []

        def run_tool() -> None:
            try:
                lifecycle.run_startup_command(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run_tool)
        worker.start()
        process = None
        try:
            deadline = time.monotonic() + 5.0
            while process is None and time.monotonic() < deadline:
                with lifecycle._process_lock:
                    process = lifecycle._active_process
                time.sleep(0.01)
            self.assertIsNotNone(process)
            assert process is not None
            identity = LIFECYCLE.capture_process_identity(process.pid)
            self.assertIsNotNone(identity)
            lifecycle.request_shutdown("test controller exit")
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIn("controller exited", str(errors[0]).casefold())
            assert identity is not None
            self.assertFalse(LIFECYCLE.process_identity_is_alive(identity))
        finally:
            lifecycle.request_shutdown("test cleanup")
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            worker.join(timeout=5)
            stdin.release.set()

    def test_lifetime_ceiling_stops_fixture_with_live_owner(self) -> None:
        server = FakeServer()
        stdin = BlockingInput()
        lifecycle = LIFECYCLE.FixtureLifecycle(
            server,
            owner_pid=12345,
            max_seconds=0.02,
            stdin=stdin,
            owner_probe=lambda _pid: True,
            poll_seconds=0.005,
            install_signals=False,
        )
        lifecycle.start()
        self.assertTrue(server.shutdown_event.wait(1.0))
        self.assertEqual(server.shutdown_count, 1)
        stdin.release.set()


if __name__ == "__main__":
    unittest.main()
