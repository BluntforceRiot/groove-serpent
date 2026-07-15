from __future__ import annotations

import io
import subprocess
import unittest
from unittest import mock

from groove_serpent.subprocess_policy import (
    BoundedDiagnostic,
    MAX_DIAGNOSTIC_BYTES,
    require_ffmpeg_nostdin,
    run_bounded_capture,
)


class SubprocessPolicyTests(unittest.TestCase):
    def test_diagnostic_drain_is_bounded_and_marks_truncation(self) -> None:
        diagnostic = BoundedDiagnostic()
        stream = io.BytesIO(b"failure detail\n" * 10_000)

        diagnostic.drain(stream)

        rendered = diagnostic.text()
        self.assertLessEqual(len(rendered), 2_050)
        self.assertIn("failure detail", rendered)
        self.assertIn("diagnostic truncated", rendered)
        self.assertTrue(stream.closed)
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

        def fake_run(command, *, check, stdin, stdout, stderr, timeout):
            captured.update(
                command=command,
                check=check,
                stdin=stdin,
                timeout=timeout,
            )
            stdout.write(b"ffmpeg version test\n" + b"x" * MAX_DIAGNOSTIC_BYTES)
            stderr.write(b"warning\n" * MAX_DIAGNOSTIC_BYTES)
            return subprocess.CompletedProcess(command, 0)

        with mock.patch(
            "groove_serpent.subprocess_policy.subprocess.run", side_effect=fake_run
        ):
            completed = run_bounded_capture(["ffmpeg", "-nostdin", "-version"])

        self.assertIs(captured["stdin"], subprocess.DEVNULL)
        self.assertFalse(captured["check"])
        self.assertIsNone(captured["timeout"])
        self.assertEqual(len(completed.stdout), MAX_DIAGNOSTIC_BYTES)
        self.assertEqual(len(completed.stderr), MAX_DIAGNOSTIC_BYTES)
        self.assertTrue(completed.stdout_truncated)
        self.assertTrue(completed.stderr_truncated)


if __name__ == "__main__":
    unittest.main()
