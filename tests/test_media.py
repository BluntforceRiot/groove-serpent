from __future__ import annotations

import io
import math
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from groove_serpent.errors import GrooveSerpentError
from groove_serpent.media import (
    decode_rms_envelope,
    local_ffmpeg_input_args,
    local_ffprobe_input_args,
    probe_audio,
    run_ffmpeg,
    tool_version,
)
from groove_serpent.subprocess_policy import (
    BoundedProcessResult,
    MEDIA_ANALYSIS_TIMEOUT_SECONDS,
    MEDIA_RENDER_TIMEOUT_SECONDS,
)


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
    def test_local_media_inputs_force_demuxers_and_local_protocols(self) -> None:
        for filename, demuxer in (
            ("side.flac", "flac"),
            ("side.wav", "wav"),
            ("side.wave", "wav"),
            ("side.aif", "aiff"),
            ("side.aiff", "aiff"),
            ("track.m4a", "mov"),
            ("cover.jpg", "image2"),
            ("cover.jpeg", "image2"),
            ("cover.png", "image2"),
        ):
            with self.subTest(filename=filename):
                path = Path(filename)
                options = [
                    "-protocol_whitelist",
                    "file,pipe",
                    "-f",
                    demuxer,
                ]
                self.assertEqual(
                    local_ffmpeg_input_args(path),
                    [*options, "-i", str(path)],
                )
                self.assertEqual(
                    local_ffprobe_input_args(path),
                    [*options, str(path)],
                )

    def test_local_media_inputs_reject_indirection_formats(self) -> None:
        for filename in ("side.ffconcat", "side.m3u", "side.m3u8"):
            for builder in (local_ffmpeg_input_args, local_ffprobe_input_args):
                with (
                    self.subTest(filename=filename, builder=builder.__name__),
                    self.assertRaisesRegex(
                        GrooveSerpentError, "Unsupported local media container"
                    ),
                ):
                    builder(Path(filename))

    def test_probe_forces_local_flac_before_reading_metadata(self) -> None:
        payload = (
            '{"streams":[{"codec_name":"flac","sample_rate":"48000",'
            '"channels":2,"bits_per_raw_sample":"24","sample_fmt":"s32",'
            '"duration":"1.0","duration_ts":"48000","time_base":"1/48000"}],'
            '"format":{"duration":"1.0"}}'
        )
        with tempfile.TemporaryDirectory() as directory_value:
            path = Path(directory_value) / "side.flac"
            path.write_bytes(b"local capture")
            completed = BoundedProcessResult(
                returncode=0,
                stdout=payload.encode("utf-8"),
                stderr=b"",
                stdout_truncated=False,
                stderr_truncated=False,
            )
            with mock.patch(
                "groove_serpent.media.find_tool", return_value="ffprobe"
            ), mock.patch(
                "groove_serpent.media.run_bounded_capture", return_value=completed
            ) as run:
                source = probe_audio(path)

        command = run.call_args.args[0]
        self.assertEqual(
            command[-5:],
            ["-protocol_whitelist", "file,pipe", "-f", "flac", str(path.resolve())],
        )
        self.assertEqual(source.codec_name, "flac")
        self.assertEqual(source.sample_count, 48_000)

    def test_error_only_ffmpeg_requires_clean_diagnostics_and_zero_exit(self) -> None:
        for return_code, diagnostic in (
            (0, b""),
            (0, b"File already exists. Exiting.\n"),
            (1, b"Encoder failed.\n"),
            (1, b""),
        ):
            with self.subTest(return_code=return_code, diagnostic=diagnostic):
                process = _FakeProcess(
                    io.BytesIO(), return_code, io.BytesIO(diagnostic)
                )
                with mock.patch(
                    "groove_serpent.media.start_media_process", return_value=process
                ) as popen:
                    command = ["ffmpeg", "-loglevel", "error", "-n", "out.m4a"]
                    if return_code or diagnostic:
                        with self.assertRaises(GrooveSerpentError):
                            run_ffmpeg(command)
                    else:
                        run_ffmpeg(command)
                self.assertTrue(process.waited)
                self.assertIn("-nostdin", popen.call_args.args[0])
                self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(
                    popen.call_args.kwargs["timeout"], MEDIA_RENDER_TIMEOUT_SECONDS
                )

    def test_analysis_emits_partial_terminal_rms_window(self) -> None:
        samples = [0.25] * 2_000 + [0.5] * 40
        process = _FakeProcess(io.BytesIO(struct.pack("<2040f", *samples)))

        with mock.patch("groove_serpent.media.find_tool", return_value="ffmpeg"), mock.patch(
            "groove_serpent.media.start_media_process", return_value=process
        ):
            values, window_seconds = decode_rms_envelope(
                Path("side.flac"),
                analysis_rate=8_000,
                window_ms=50,
                maximum_duration_seconds=1.0,
            )

        self.assertEqual(len(values), 6)
        self.assertAlmostEqual(values[0], 20.0 * math.log10(0.25))
        self.assertAlmostEqual(values[-1], 20.0 * math.log10(0.5))
        self.assertEqual(window_seconds, 0.05)

    def test_ffmpeg_version_query_uses_shared_noninteractive_bounded_policy(self) -> None:
        result = BoundedProcessResult(
            returncode=0,
            stdout=b"ffmpeg version bounded-test\n",
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
        )
        with mock.patch("groove_serpent.media.find_tool", return_value="ffmpeg"), mock.patch(
            "groove_serpent.media.run_bounded_capture", return_value=result
        ) as bounded:
            version = tool_version("ffmpeg")

        self.assertEqual(version, "ffmpeg version bounded-test")
        self.assertIn("-nostdin", bounded.call_args.args[0])

    def test_analysis_stderr_is_drained_with_a_bounded_capture(self) -> None:
        captured: dict[str, object] = {}

        def popen(command, *, timeout, stdin, stdout, stderr):
            captured["command"] = command
            captured["timeout"] = timeout
            captured["stdin"] = stdin
            captured["stdout"] = stdout
            captured["stderr"] = stderr
            return _FakeProcess(
                io.BytesIO(struct.pack("<f", 0.25)),
                return_code=1,
                stderr=io.BytesIO(b"diagnostic\n" * 10_000),
            )

        with mock.patch("groove_serpent.media.find_tool", return_value="ffmpeg"), mock.patch(
            "groove_serpent.media.start_media_process", side_effect=popen
        ):
            with self.assertRaisesRegex(
                GrooveSerpentError, "diagnostic"
            ) as raised:
                decode_rms_envelope(
                    Path("side.flac"),
                    analysis_rate=10,
                    window_ms=100,
                    maximum_duration_seconds=1.0,
                )

        self.assertIn("-nostdin", captured["command"])
        command = cast(list[str], captured["command"])
        input_index = command.index("-i")
        self.assertEqual(
            command[input_index - 4 : input_index],
            ["-protocol_whitelist", "file,pipe", "-f", "flac"],
        )
        self.assertIn("-fs", captured["command"])
        self.assertIs(captured["stdin"], subprocess.DEVNULL)
        self.assertIs(captured["stdout"], subprocess.PIPE)
        self.assertIs(captured["stderr"], subprocess.PIPE)
        self.assertEqual(captured["timeout"], MEDIA_ANALYSIS_TIMEOUT_SECONDS)
        self.assertIn("[diagnostic truncated]", str(raised.exception))
        self.assertLess(len(str(raised.exception)), 70_000)

    def test_analysis_exception_terminates_and_reaps_ffmpeg(self) -> None:
        process = _FakeProcess(_ExplodingStream())

        with mock.patch("groove_serpent.media.find_tool", return_value="ffmpeg"), mock.patch(
            "groove_serpent.media.start_media_process", return_value=process
        ):
            with self.assertRaisesRegex(RuntimeError, "decode loop failed"):
                decode_rms_envelope(
                    Path("side.flac"),
                    analysis_rate=10,
                    window_ms=100,
                    maximum_duration_seconds=1.0,
                )

        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)
        self.assertTrue(process.stdout.closed)

    def test_analysis_refuses_pcm_beyond_validated_duration(self) -> None:
        process = _FakeProcess(io.BytesIO(struct.pack("<20f", *([0.25] * 20))))

        with mock.patch("groove_serpent.media.find_tool", return_value="ffmpeg"), mock.patch(
            "groove_serpent.media.start_media_process", return_value=process
        ), self.assertRaisesRegex(GrooveSerpentError, "validated PCM output bound"):
            decode_rms_envelope(
                Path("side.flac"),
                analysis_rate=10,
                window_ms=100,
                maximum_duration_seconds=1.0,
            )

        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)


if __name__ == "__main__":
    unittest.main()
