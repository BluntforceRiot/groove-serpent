from __future__ import annotations

import importlib.util
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "browser_acceptance_runner", ROOT / "scripts" / "run_browser_acceptance.py"
)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class FakeProcess:
    def __init__(self, *, running: bool = True, stubborn: bool = False) -> None:
        self.running = running
        self.stubborn = stubborn
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self.running else 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, *, timeout: int) -> int:
        assert timeout == 5
        if self.stubborn and not self.killed:
            raise subprocess.TimeoutExpired("owned-pulseaudio", timeout)
        self.running = False
        return 0


def test_missing_executable_and_non_linux_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RUNNER.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="requires pactl on PATH"):
        RUNNER._executable("pactl")
    monkeypatch.setattr(RUNNER.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="Linux hosted browser job only"):
        with RUNNER.native_audio_environment():
            pytest.fail("Unsupported bootstrap must not reach browser acceptance.")


@pytest.mark.parametrize("stubborn", [False, True])
def test_only_owned_process_is_terminated(stubborn: bool) -> None:
    process = FakeProcess(stubborn=stubborn)
    RUNNER._stop_owned_process(process)
    assert process.terminated
    assert process.killed is stubborn
    assert not process.running
    finished = FakeProcess(running=False)
    RUNNER._stop_owned_process(finished)
    assert not finished.terminated


def test_readiness_requires_exact_sink_not_a_successful_pactl_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = iter([
        "0\tgroove_serpent_ci_wrong\tmodule-null-sink.c\ts16le\tIDLE\n",
        "0\tgroove_serpent_ci\tmodule-null-sink.c\ts16le\tIDLE\n",
    ])
    calls = []

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=next(outputs), stderr="")

    monkeypatch.setattr(RUNNER.subprocess, "run", run)
    monkeypatch.setattr(RUNNER.time, "sleep", lambda _seconds: None)
    RUNNER._wait_for_sink(FakeProcess(), "/usr/bin/pactl", {"PULSE_SERVER": "owned"})
    assert len(calls) == 2
    assert calls[0][0] == ["/usr/bin/pactl", "list", "short", "sinks"]
    assert calls[0][1]["env"] == {"PULSE_SERVER": "owned"}


def test_readiness_timeout_and_dead_process_do_not_start_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="exited during startup"):
        RUNNER._wait_for_sink(FakeProcess(running=False), "pactl", {})
    times = iter([0, 0, 11])
    monkeypatch.setattr(RUNNER.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(RUNNER.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        RUNNER.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="refused"),
    )
    with pytest.raises(RuntimeError, match="did not become ready: refused"):
        RUNNER._wait_for_sink(FakeProcess(), "pactl", {})


@pytest.mark.parametrize("failure", [None, "probe", "browser"])
def test_private_audio_environment_probes_and_always_cleans_its_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str | None,
) -> None:
    process = FakeProcess()
    calls = []
    launches = []
    monkeypatch.setattr(RUNNER.sys, "platform", "linux")
    monkeypatch.setattr(RUNNER, "_executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        RUNNER.tempfile, "TemporaryDirectory", lambda **_kwargs: nullcontext(str(tmp_path)),
    )
    monkeypatch.setenv("PULSE_SERVER", "existing-user-server")
    monkeypatch.setattr(
        RUNNER.subprocess, "Popen",
        lambda *args, **kwargs: launches.append((args, kwargs)) or process,
    )

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        if command[0].endswith("paplay") and failure == "probe":
            raise subprocess.CalledProcessError(1, command)
        return SimpleNamespace(
            returncode=0, stdout="0\tgroove_serpent_ci\tmodule-null-sink.c\n", stderr="",
        )

    monkeypatch.setattr(RUNNER.subprocess, "run", run)

    def execute() -> None:
        with RUNNER.native_audio_environment() as environment:
            assert environment["PULSE_SERVER"] == f"unix:{tmp_path / 'native'}"
            assert environment["PULSE_SINK"] == "groove_serpent_ci"
            assert RUNNER.os.environ["PULSE_SERVER"] == "existing-user-server"
            if failure == "browser":
                raise ValueError("browser failed")

    if failure is None:
        execute()
    else:
        expected = {
            "probe": subprocess.CalledProcessError,
            "browser": ValueError,
        }[failure]
        with pytest.raises(expected):
            execute()
    assert process.terminated
    assert not process.running
    command = launches[0][0][0]
    assert "-n" in command and "--daemonize=no" in command and "--use-pid-file=no" in command
    assert any("module-native-protocol-unix" in item for item in command)
    assert any("module-null-sink sink_name=groove_serpent_ci" in item for item in command)
    assert len(calls) == 2
    assert calls[1][0][0].endswith("paplay")
    assert calls[1][1]["input"] == bytes(38400)
    assert calls[1][1]["check"] is True


@pytest.mark.parametrize("engine", RUNNER.ENGINES)
@pytest.mark.parametrize("returncode", [0, 1, 17])
def test_playwright_engine_cwd_environment_and_exit_are_preserved(
    monkeypatch: pytest.MonkeyPatch, engine: str, returncode: int,
) -> None:
    environment = {"PULSE_SERVER": "owned-socket"}
    calls = []
    monkeypatch.setattr(RUNNER.sys, "argv", ["runner", "--engine", engine])
    monkeypatch.setattr(RUNNER, "_executable", lambda _name: "/usr/bin/npx")
    monkeypatch.setattr(RUNNER, "native_audio_environment", lambda: nullcontext(environment))
    monkeypatch.setattr(
        RUNNER.subprocess, "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or SimpleNamespace(returncode=returncode),
    )
    assert RUNNER.main() == returncode
    assert calls == [(
        ["/usr/bin/npx", "--no-install", "playwright", "test", f"--project={engine}"],
        {"cwd": ROOT, "env": environment, "check": False},
    )]
