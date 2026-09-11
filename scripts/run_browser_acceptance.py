"""Run Linux browser acceptance against an owned, verified native audio sink.

A headless runner still needs an audio output service for native media playback.
The null sink supplies a real media clock, not mocked playback or audition credit.
No desktop/user PulseAudio daemon is changed or terminated by this helper.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SINK_NAME = "groove_serpent_ci"
ENGINES = ("chromium", "firefox", "webkit", "mobile-chromium")
ROOT = Path(__file__).resolve().parent.parent


def _executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"Browser acceptance requires {name} on PATH.")
    return executable


def _stop_owned_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _wait_for_sink(
    process: subprocess.Popen[bytes], pactl: str, environment: dict[str, str]
) -> None:
    deadline = time.monotonic() + 10
    last_result = "The audio service has not answered."
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("The owned PulseAudio process exited during startup.")
        try:
            result = subprocess.run(
                [pactl, "list", "short", "sinks"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            last_result = result.stderr.strip() or result.stdout.strip()
            names = [
                line.split()[1] for line in result.stdout.splitlines() if len(line.split()) > 1
            ]
            if result.returncode == 0 and SINK_NAME in names:
                return
        except subprocess.TimeoutExpired:
            last_result = "pactl timed out."
        time.sleep(0.1)
    raise RuntimeError(f"The owned PulseAudio sink did not become ready: {last_result}")


@contextmanager
def native_audio_environment() -> Iterator[dict[str, str]]:
    if sys.platform != "linux":
        raise RuntimeError("This audio bootstrap is for the Linux hosted browser job only.")
    pulseaudio = _executable("pulseaudio")
    pactl = _executable("pactl")
    paplay = _executable("paplay")
    # A short private directory avoids the Unix-domain socket path-length limit.
    with tempfile.TemporaryDirectory(prefix="gs-audio-", dir="/tmp") as temporary:
        directory = Path(temporary)
        directory.chmod(0o700)
        environment = {
            **os.environ,
            "PULSE_SERVER": f"unix:{directory / 'native'}",
            "PULSE_SINK": SINK_NAME,
            "PULSE_RUNTIME_PATH": str(directory),
        }
        with (directory / "pulseaudio.log").open("w+b") as log:
            process = subprocess.Popen(
                [
                    pulseaudio,
                    "--daemonize=no",
                    "--exit-idle-time=-1",
                    "--use-pid-file=no",
                    "--log-target=stderr",
                    "-n",
                    "--load=module-native-protocol-unix "
                    f"socket={directory / 'native'} auth-anonymous=1",
                    f"--load=module-null-sink sink_name={SINK_NAME} rate=48000 channels=2",
                ],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                _wait_for_sink(process, pactl, environment)
                # Prove an actual client can drain PCM through the selected sink.
                subprocess.run(
                    [paplay, "--raw", "--format=s16le", "--rate=48000", "--channels=2"],
                    input=bytes(48000 * 2 * 2 // 5),
                    env=environment,
                    check=True,
                    timeout=10,
                )
                print(
                    f"Native audio sink ready: {SINK_NAME}; PCM playback probe passed.", flush=True
                )
                yield environment
            finally:
                _stop_owned_process(process)
                log.flush()
                log.seek(0)
                diagnostic = log.read().decode("utf-8", errors="replace")
                if diagnostic:
                    print("Owned PulseAudio diagnostics:\n" + diagnostic, file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=ENGINES, required=True)
    args = parser.parse_args()
    npx = _executable("npx")
    with native_audio_environment() as environment:
        result = subprocess.run(
            [npx, "--no-install", "playwright", "test", f"--project={args.engine}"],
            cwd=ROOT,
            env=environment,
            check=False,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
