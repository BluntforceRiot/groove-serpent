from __future__ import annotations

import io
import struct
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from groove_serpent.errors import GrooveSerpentError
from groove_serpent.media import decode_rms_envelope, tool_version


class _FakeProcess:
    def __init__(
        self,
        stdout: io.BytesIO,
        return_code: int = 0,
        stderr: io.BytesIO | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr or io.BytesIO()
        self.return_code = return_code
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self) -> int | None:
        return self.return_code if self.waited else None

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = -15

    def kill(self) -> None:
        self.killed = True
        self.return_code = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.waited = True
        return self.return_code


class _ExplodingStream(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        del size
        raise RuntimeError("decode loop failed")


class MediaTests(unittest.TestCase):
    def test_ffmpeg_version_query_uses_shared_noninteractive_bounded_policy(self) -> None:
        observed: dict[str, object] = {}

        def fake_run(command, *, check, stdin, stdout, stderr):
            observed.update(command=command, check=check, stdin=stdin)
            stdout.write(b"ffmpeg version bounded-test\n")
            return subprocess.CompletedProcess(command, 0)

        with mock.patch("groove_serpent.media.find_tool", return_value="ffmpeg"), mock.patch(
            "groove_serpent.subprocess_policy.subprocess.run", side_effect=fake_run
        ):
            version = tool_version("ffmpeg")

        self.assertEqual(version, "ffmpeg version bounded-test")
        self.assertIn("-nostdin", observed["command"])
        self.assertIs(observed["stdin"], subprocess.DEVNULL)

    def test_analysis_stderr_is_drained_with_a_bounded_capture(self) -> None:
        captured: dict[str, object] = {}

        def popen(command, *, stdin, stdout, stderr):
            captured["command"] = command
            captured["stdin"] = stdin
            captured["stdout"] = stdout
            captured["stderr"] = stderr
            return _FakeProcess(
                io.BytesIO(struct.pack("<f", 0.25)),
                return_code=1,
                stderr=io.BytesIO(b"diagnostic\n" * 10_000),
            )

        with mock.patch("groove_serpent.media.find_tool", return_value="ffmpeg"), mock.patch(
            "groove_serpent.media.subprocess.Popen", side_effect=popen
        ):
            with self.assertRaisesRegex(
                GrooveSerpentError, "diagnostic"
            ) as raised:
                decode_rms_envelope(Path("side.flac"), analysis_rate=10, window_ms=100)

        self.assertIn("-nostdin", captured["command"])
        self.assertIs(captured["stdin"], subprocess.DEVNULL)
        self.assertIs(captured["stdout"], subprocess.PIPE)
        self.assertIs(captured["stderr"], subprocess.PIPE)
        self.assertIn("[diagnostic truncated]", str(raised.exception))
        self.assertLess(len(str(raised.exception)), 70_000)

    def test_analysis_exception_terminates_and_reaps_ffmpeg(self) -> None:
        process = _FakeProcess(_ExplodingStream())

        with mock.patch("groove_serpent.media.find_tool", return_value="ffmpeg"), mock.patch(
            "groove_serpent.media.subprocess.Popen", return_value=process
        ):
            with self.assertRaisesRegex(RuntimeError, "decode loop failed"):
                decode_rms_envelope(Path("side.flac"), analysis_rate=10, window_ms=100)

        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)
        self.assertTrue(process.stdout.closed)


if __name__ == "__main__":
    unittest.main()
