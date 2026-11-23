from __future__ import annotations

import io
import math
import os
import subprocess
import unittest
from unittest import mock

from groove_serpent.subprocess_policy import (
    BoundedDiagnostic,
    MAX_DIAGNOSTIC_BYTES,
    MEDIA_QUERY_TIMEOUT_SECONDS,
    require_ffmpeg_nostdin,
    run_bounded_capture,
    start_media_process,
)


class SubprocessPolicyTests(unittest.TestCase):
    def test_diagnostic_drain_is_bounded_and_marks_truncation(self) -> None:
        overflows: list[bool] = []
        diagnostic = BoundedDiagnostic(
            overflow_callback=lambda: overflows.append(True)
        )
        stream = io.BytesIO(b"failure detail\n" * 10_000)

        diagnostic.drain(stream)

        rendered = diagnostic.text()
        self.assertLessEqual(len(rendered), 2_050)
        self.assertIn("failure detail", rendered)
        self.assertIn("diagnostic truncated", rendered)
        self.assertTrue(stream.closed)
        self.assertEqual(overflows, [True])
        self.assertGreater(10_000 * len(b"failure detail\n"), MAX_DIAGNOSTIC_BYTES)

    def test_ffmpeg_commands_are_noninteractive(self) -> None:
        original = ["ffmpeg", "-hide_banner", "-i", "side.flac"]
        rendered = require_ffmpeg_nostdin(original)
        self.assertEqual(rendered[1], "-nostdin")
        self.assertEqual(original, ["ffmpeg", "-hide_banner", "-i", "side.flac"])
        self.assertEqual(require_ffmpeg_nostdin(rendered).count("-nostdin"), 1)

        with self.assertRaisesRegex(ValueError, "not FFmpeg"):
            require_ffmpeg_nostdin(["fpcalc", "-"])

    def test_short_media_capture_uses_no_stdin_and_bounds_both_streams(self) -> None:
        captured: dict[str, object] = {}

        class FakeProcess:
            def __init__(self) -> None:
                self.stdout = io.BytesIO(
                    b"ffmpeg version test\n" + b"x" * MAX_DIAGNOSTIC_BYTES
                )
                self.stderr = io.BytesIO(b"warning\n" * MAX_DIAGNOSTIC_BYTES)
                self.returncode: int | None = None
                self.terminated = False
                self.killed = False

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = -15

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                return self.returncode if self.returncode is not None else 0

        process = FakeProcess()

        def fake_start(command, *, timeout, stdin, stdout, stderr):
            captured.update(
                command=command,
                stdin=stdin,
                timeout=timeout,
                stdout=stdout,
                stderr=stderr,
            )
            return process

        with mock.patch(
            "groove_serpent.subprocess_policy.start_media_process",
            side_effect=fake_start,
        ):
            completed = run_bounded_capture(["ffmpeg", "-nostdin", "-version"])

        self.assertIs(captured["stdin"], subprocess.DEVNULL)
        self.assertIs(captured["stdout"], subprocess.PIPE)
        self.assertIs(captured["stderr"], subprocess.PIPE)
        self.assertEqual(captured["timeout"], MEDIA_QUERY_TIMEOUT_SECONDS)
        self.assertEqual(len(completed.stdout), MAX_DIAGNOSTIC_BYTES)
        self.assertEqual(len(completed.stderr), MAX_DIAGNOSTIC_BYTES)
        self.assertTrue(completed.stdout_truncated)
        self.assertTrue(completed.stderr_truncated)
        self.assertTrue(process.terminated)

    def test_media_launcher_owns_scope_and_rejects_infinite_deadlines(self) -> None:
        for invalid in (0.0, -1.0, math.inf, math.nan):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                start_media_process(["ffmpeg"], timeout=invalid)

        with mock.patch("groove_serpent.subprocess_policy._DeadlineProcess") as launcher:
            start_media_process(
                ["ffmpeg", "-version"],
                timeout=12.0,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        launched = launcher.call_args.args[0]
        kwargs = launcher.call_args.kwargs
        self.assertEqual(kwargs["original_command"], ["ffmpeg", "-version"])
        self.assertEqual(kwargs["media_timeout"], 12.0)
        if os.name == "nt":
            self.assertIn("KILL_ON_JOB_CLOSE", launched[3])
            self.assertEqual(launched[-2:], ["ffmpeg", "-version"])
        else:
            self.assertEqual(launched, ["ffmpeg", "-version"])
            self.assertTrue(kwargs["start_new_session"])


if __name__ == "__main__":
    unittest.main()
