from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Callable
from unittest import mock

from groove_serpent import __user_agent__
from groove_serpent.errors import ProjectValidationError
from groove_serpent.recognition import (
    ACOUSTID_LOOKUP_URL,
    AcoustIDRecognitionProvider,
    FingerprintBackendReadiness,
    NoRecognitionProvider,
    RecognitionError,
    RecognitionMatch,
    RecognitionProvider,
    RecognitionReadiness,
    _FingerprintRuntime,
    _capture_executable_identity,
    _discover_fingerprint_runtime,
    _excerpt_sample_bounds,
    _find_fpcalc,
    _fingerprint_duration,
    _parse_fingerprint_output,
    _parse_matches,
    _probe_ffmpeg_chromaprint,
)
from groove_serpent.subprocess_policy import BoundedProcessResult


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        wait_timeout: bool = False,
        on_wait: Callable[[], None] | None = None,
    ) -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self._wait_timeout = wait_timeout
        self._on_wait = on_wait
        self.terminated = False
        self.killed = False
        self.waited = False

    def communicate(
        self,
        input: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        del input, timeout
        if self._wait_timeout and not self.terminated:
            raise subprocess.TimeoutExpired("fpcalc", 1)
        return self.stdout.read(), self.stderr.read()

    def __enter__(self) -> _FakeProcess:
        return self

    def __exit__(self, *args: object) -> None:
        if self.poll() is None:
            self.kill()
        self.wait()

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.waited = True
        if self._on_wait is not None:
            callback, self._on_wait = self._on_wait, None
            callback()
        if self._wait_timeout and not self.terminated:
            raise subprocess.TimeoutExpired("fingerprint", 1)
        return self.returncode

    def poll(self) -> int | None:
        return None if self._wait_timeout and not self.terminated else self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._raw[:size]


def _runtime(
    root: Path,
    backend: str,
) -> _FingerprintRuntime:
    ffmpeg_path = root / "ffmpeg.exe"
    ffmpeg_path.write_bytes(b"synthetic ffmpeg executable")
    ffmpeg = _capture_executable_identity(str(ffmpeg_path), "FFmpeg executable")
    if backend == "ffmpeg-chromaprint":
        return _FingerprintRuntime("ffmpeg-chromaprint", ffmpeg)
    fpcalc_path = root / "fpcalc.exe"
    fpcalc_path.write_bytes(b"synthetic fpcalc executable")
    fpcalc = _capture_executable_identity(str(fpcalc_path), "fpcalc executable")
    return _FingerprintRuntime("fpcalc", ffmpeg, fpcalc)


def _runtime_status(runtime: _FingerprintRuntime) -> FingerprintBackendReadiness:
    return FingerprintBackendReadiness(
        ready=True,
        backend=runtime.backend,
        message="ready",
        ffmpeg=runtime.ffmpeg.path,
        fpcalc=runtime.fpcalc.path if runtime.fpcalc else "",
        direct_capability=("ready" if runtime.backend == "ffmpeg-chromaprint" else "absent"),
    )


class RecognitionDataTests(unittest.TestCase):
    def test_readiness_and_match_are_json_ready(self) -> None:
        readiness = RecognitionReadiness(
            provider="test",
            enabled=True,
            ready=False,
            message="missing runtime",
            missing=("fpcalc",),
        )
        match = RecognitionMatch(
            title="Song",
            artist_credit="Artist A & Artist B",
            score=0.92,
            recording_mbid="recording-id",
            release_candidates=(
                {
                    "release_mbid": "release-id",
                    "title": "Album",
                    "release_group_mbid": "group-id",
                },
            ),
            release_group_ids=("group-id",),
        )
        self.assertEqual(readiness.to_dict()["missing"], ["fpcalc"])
        self.assertEqual(match.to_dict()["artist_credit"], "Artist A & Artist B")
        self.assertEqual(match.to_dict()["release_group_ids"], ["group-id"])
        json.dumps({"readiness": readiness.to_dict(), "match": match.to_dict()})

    def test_no_provider_is_a_safe_protocol_implementation(self) -> None:
        provider = NoRecognitionProvider()
        self.assertIsInstance(provider, RecognitionProvider)
        self.assertFalse(provider.readiness().ready)
        self.assertEqual(provider.identify_track("unused.flac", 0, 1, 1), [])

    def test_parse_and_rank_acoustid_recordings(self) -> None:
        payload = {
            "status": "ok",
            "results": [
                {
                    "score": 0.71,
                    "recordings": [
                        {"id": "low", "title": "Lower", "artists": [{"name": "Solo"}]}
                    ],
                },
                {
                    "score": 0.97,
                    "recordings": [
                        {
                            "id": "high",
                            "title": "Higher",
                            "artists": [
                                {"name": "One", "joinphrase": " feat. "},
                                {"name": "Two", "joinphrase": ""},
                            ],
                            "releasegroups": [
                                {
                                    "id": "group-1",
                                    "title": "Album Group",
                                    "type": "Album",
                                    "releases": [
                                        {
                                            "id": "release-1",
                                            "title": "Album",
                                            "country": "US",
                                            "date": "1980-01-02",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        matches = _parse_matches(payload)
        self.assertEqual([item.recording_mbid for item in matches], ["high", "low"])
        self.assertEqual(matches[0].artist_credit, "One feat. Two")
        self.assertEqual(matches[0].release_group_ids, ("group-1",))
        self.assertEqual(
            matches[0].release_candidates[0]["release_mbid"], "release-1"
        )


class RecognitionDiscoveryTests(unittest.TestCase):
    def test_key_is_opt_in_and_missing_runtime_does_not_raise(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "groove_serpent.recognition.find_executable", return_value=None
        ):
            disabled = AcoustIDRecognitionProvider()
            self.assertFalse(disabled.readiness().enabled)
            enabled = AcoustIDRecognitionProvider(api_key="client-key")
            status = enabled.readiness()
        self.assertTrue(status.enabled)
        self.assertFalse(status.ready)
        self.assertEqual(status.missing, ("fpcalc", "ffmpeg"))

    def test_readiness_contains_runtime_discovery_failures(self) -> None:
        provider = AcoustIDRecognitionProvider(api_key="client-key")
        with mock.patch(
            "groove_serpent.recognition._find_fpcalc",
            side_effect=OSError("broken search path"),
        ), mock.patch(
            "groove_serpent.recognition.find_executable",
            side_effect=ValueError("broken PATH"),
        ):
            status = provider.readiness()
        self.assertFalse(status.ready)
        self.assertEqual(status.missing, ("fpcalc", "ffmpeg"))

    def test_key_can_only_come_from_dedicated_environment_variable(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ACOUSTID_API_KEY": "wrong-place",
                "GROOVE_SERPENT_ACOUSTID_KEY": "right-place",
            },
            clear=True,
        ):
            provider = AcoustIDRecognitionProvider()
        self.assertTrue(provider.readiness().enabled)

    def test_fpcalc_environment_override_precedes_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path_directory = root / "path"
            path_directory.mkdir()
            suffix = ".exe" if os.name == "nt" else ""
            executable = root / f"fp calc{suffix}"
            executable.write_bytes(b"configured")
            path_executable = path_directory / f"fpcalc{suffix}"
            path_executable.write_bytes(b"path")
            if os.name != "nt":
                executable.chmod(0o755)
                path_executable.chmod(0o755)
            with mock.patch.dict(
                os.environ,
                {
                    "GROOVE_SERPENT_FPCALC": str(executable),
                    "PATH": str(path_directory),
                    "PATHEXT": ".EXE",
                },
                clear=True,
            ):
                self.assertEqual(_find_fpcalc(), str(executable.resolve()))


class FingerprintBackendTests(unittest.TestCase):
    def test_capability_probe_requires_exact_bounded_muxer_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = _runtime(Path(temp_dir), "ffmpeg-chromaprint")
            ready = BoundedProcessResult(
                0,
                (
                    b"Muxer chromaprint [Chromaprint]:\n"
                    b"Default audio codec: pcm_s16le.\n"
                    b"-fp_format base64\n"
                ),
                b"",
                False,
                False,
            )
            with mock.patch(
                "groove_serpent.recognition.run_bounded_capture",
                return_value=ready,
            ) as run:
                state, _message = _probe_ffmpeg_chromaprint(runtime.ffmpeg)
            self.assertEqual(state, "ready")
            command = run.call_args.args[0]
            self.assertEqual(command[-2:], ["-h", "muxer=chromaprint"])
            self.assertIn("-nostdin", command)
            self.assertEqual(run.call_args.kwargs["timeout"], 10.0)

            malformed = BoundedProcessResult(0, b"chromaprint maybe", b"", False, False)
            with mock.patch(
                "groove_serpent.recognition.run_bounded_capture",
                return_value=malformed,
            ):
                state, _message = _probe_ffmpeg_chromaprint(runtime.ffmpeg)
            self.assertEqual(state, "malformed")

            with mock.patch(
                "groove_serpent.recognition.run_bounded_capture",
                side_effect=subprocess.TimeoutExpired("ffmpeg", 10),
            ):
                state, _message = _probe_ffmpeg_chromaprint(runtime.ffmpeg)
            self.assertEqual(state, "timeout")

    def test_confirmed_absence_uses_fpcalc_but_malformed_does_not_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fallback = _runtime(root, "fpcalc")
            with (
                mock.patch(
                    "groove_serpent.recognition.find_executable",
                    return_value=fallback.ffmpeg.path,
                ),
                mock.patch(
                    "groove_serpent.recognition._find_fpcalc",
                    return_value=fallback.fpcalc.path,
                ),
                mock.patch(
                    "groove_serpent.recognition._probe_ffmpeg_chromaprint",
                    return_value=("absent", "not compiled"),
                ),
                mock.patch("groove_serpent.recognition.urllib.request.urlopen") as network,
            ):
                status, runtime = _discover_fingerprint_runtime()
            self.assertTrue(status.ready)
            self.assertEqual(status.backend, "fpcalc")
            self.assertIsNotNone(runtime)
            network.assert_not_called()

            with (
                mock.patch(
                    "groove_serpent.recognition.find_executable",
                    return_value=fallback.ffmpeg.path,
                ),
                mock.patch(
                    "groove_serpent.recognition._find_fpcalc",
                    return_value=fallback.fpcalc.path,
                ) as find_fpcalc,
                mock.patch(
                    "groove_serpent.recognition._probe_ffmpeg_chromaprint",
                    return_value=("malformed", "untrusted output"),
                ),
            ):
                status, runtime = _discover_fingerprint_runtime()
            self.assertFalse(status.ready)
            self.assertIsNone(runtime)
            find_fpcalc.assert_not_called()

    def test_both_backends_absent_is_bounded_and_network_free(self) -> None:
        with (
            mock.patch("groove_serpent.recognition.find_executable", return_value=None),
            mock.patch("groove_serpent.recognition._find_fpcalc", return_value=None),
            mock.patch("groove_serpent.recognition.urllib.request.urlopen") as network,
        ):
            status, runtime = _discover_fingerprint_runtime()
        self.assertFalse(status.ready)
        self.assertEqual(status.missing, ("fpcalc", "ffmpeg"))
        self.assertIsNone(runtime)
        network.assert_not_called()

    def test_direct_fingerprint_is_one_child_exact_range_and_geometry_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.flac"
            source.touch()
            runtime = _runtime(root, "ffmpeg-chromaprint")
            process = _FakeProcess(stdout=b"AQAA-valid_fingerprint")
            commands: list[list[str]] = []

            def popen(command: list[str], **kwargs: object) -> _FakeProcess:
                del kwargs
                commands.append(command)
                return process

            provider = AcoustIDRecognitionProvider(api_key="key")
            with mock.patch(
                "groove_serpent.recognition.subprocess.Popen",
                side_effect=popen,
            ), mock.patch("groove_serpent.recognition.urllib.request.urlopen") as network:
                payload = provider._fingerprint(
                    source,
                    123,
                    123 + 44_100 * 10 + 22_050,
                    44_100,
                    runtime=runtime,
                )
            self.assertEqual(payload["fingerprint"], "AQAA-valid_fingerprint")
            self.assertEqual(payload["duration"], 11)
            self.assertEqual(len(commands), 1)
            self.assertIn(
                "atrim=start_sample=123:end_sample=463173,asetpts=PTS-STARTPTS",
                commands[0],
            )
            self.assertIn("chromaprint", commands[0])
            self.assertNotIn("wav", commands[0])
            self.assertTrue(process.waited)
            network.assert_not_called()

    def test_direct_failure_timeout_swap_and_output_bounds_reap(self) -> None:
        cases = ("failure", "timeout", "swap", "invalid", "oversize")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source = root / "source.flac"
                source.touch()
                runtime = _runtime(root, "ffmpeg-chromaprint")

                def swap() -> None:
                    Path(runtime.ffmpeg.path).write_bytes(b"replacement executable")

                process = _FakeProcess(
                    stdout=(
                        b"A" * (64 * 1024 + 1)
                        if case == "oversize"
                        else b"invalid fingerprint!"
                        if case == "invalid"
                        else b"AQAA-valid"
                    ),
                    stderr=b"backend failure",
                    returncode=1 if case == "failure" else 0,
                    wait_timeout=case == "timeout",
                    on_wait=swap if case == "swap" else None,
                )
                provider = AcoustIDRecognitionProvider(api_key="key")
                with mock.patch(
                    "groove_serpent.recognition.subprocess.Popen",
                    return_value=process,
                ), self.assertRaises(RecognitionError):
                    provider._fingerprint(
                        source,
                        0,
                        44_100 * 10,
                        44_100,
                        runtime=runtime,
                    )
                self.assertTrue(process.waited)
                if case == "timeout":
                    self.assertTrue(process.terminated)

    def test_duration_is_integer_geometry_not_backend_metadata(self) -> None:
        self.assertEqual(_fingerprint_duration(0, 44_100 * 10 + 22_050, 44_100), 11)
        self.assertEqual(_fingerprint_duration(0, 44_100 * 10 + 22_049, 44_100), 10)
        self.assertEqual(
            _fingerprint_duration(
                0,
                22_050 * 10,
                44_100,
                source_speed_factor=2.0,
            ),
            10,
        )
        self.assertEqual(
            _fingerprint_duration(
                0,
                88_200 * 10,
                44_100,
                source_speed_factor=0.5,
            ),
            10,
        )
        with self.assertRaises(ValueError):
            _fingerprint_duration(10, 10, 44_100)
        invalid_factors = (
            ("boolean", True),
            ("too-small", 0.24),
            ("too-large", 2.01),
            ("nan", float("nan")),
            ("unrepresentable-integer", 10**10_000),
        )
        for label, factor in invalid_factors:
            with self.subTest(factor=label), self.assertRaises(ValueError):
                _fingerprint_duration(
                    0,
                    44_100,
                    44_100,
                    source_speed_factor=factor,
                )

    def test_direct_fingerprint_applies_reviewed_pitch_and_tempo_speed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.flac"
            source.touch()
            runtime = _runtime(root, "ffmpeg-chromaprint")
            process = _FakeProcess(stdout=b"AQAA-speed-corrected")
            commands: list[list[str]] = []

            def popen(command: list[str], **kwargs: object) -> _FakeProcess:
                del kwargs
                commands.append(command)
                return process

            provider = AcoustIDRecognitionProvider(api_key="key")
            with mock.patch(
                "groove_serpent.recognition.subprocess.Popen",
                side_effect=popen,
            ):
                payload = provider._fingerprint(
                    source,
                    0,
                    42_425 * 10,
                    44_100,
                    runtime=runtime,
                    source_speed_factor=1.039482143,
                )
            self.assertEqual(payload["duration"], 10)
            self.assertEqual(len(commands), 1)
            self.assertIn(
                "atrim=start_sample=0:end_sample=424250,"
                "asetpts=PTS-STARTPTS,asetrate=42425",
                commands[0],
            )

    def test_fpcalc_json_is_strict_bounded_and_duration_is_not_authoritative(self) -> None:
        payload = _parse_fingerprint_output(
            b'{"duration":1,"fingerprint":"AQAA-valid"}',
            authoritative_duration=120,
        )
        self.assertEqual(payload, {"fingerprint": "AQAA-valid", "duration": 120})
        invalid = (
            b'{"duration":1,"duration":2,"fingerprint":"AQAA-valid"}',
            b'{"duration":NaN,"fingerprint":"AQAA-valid"}',
            b'{"duration":1,"fingerprint":"bad value"}',
            b'{"duration":1,"fingerprint":"AQAA-valid","extra":true}',
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(RecognitionError):
                _parse_fingerprint_output(raw, authoritative_duration=120)


class RecognitionPipelineTests(unittest.TestCase):
    def test_excerpt_skips_lead_and_is_capped_at_120_seconds(self) -> None:
        start, end = _excerpt_sample_bounds(44_100, 44_100 * 300, 44_100)
        self.assertEqual(start, 44_100 * 9)
        self.assertEqual(end - start, 44_100 * 120)

        short_start, short_end = _excerpt_sample_bounds(0, 44_100 * 10, 44_100)
        self.assertEqual((short_start, short_end), (0, 44_100 * 10))

        corrected_start, corrected_end = _excerpt_sample_bounds(
            0,
            44_100 * 300,
            44_100,
            source_speed_factor=1.039482143,
        )
        self.assertEqual(corrected_start, 42_425 * 8)
        self.assertEqual(corrected_end - corrected_start, 42_425 * 120)

    def test_pipeline_pipes_pcm_wav_to_fpcalc_and_posts_lookup(self) -> None:
        lookup_payload = {
            "status": "ok",
            "results": [
                {
                    "score": 0.88,
                    "recordings": [
                        {
                            "id": "recording-id",
                            "title": "Recognized Song",
                            "artists": [{"name": "Recognized Artist"}],
                        }
                    ],
                }
            ],
        }
        ffmpeg_process = _FakeProcess()
        fpcalc_process = _FakeProcess(
            stdout=json.dumps(
                # Non-seekable PCM WAV input can make fpcalc 1.6 report zero;
                # the exact excerpt sample count supplies the duration.
                {"duration": 0.0, "fingerprint": "encoded-fingerprint"}
            ).encode("utf-8")
        )
        popen_calls: list[list[str]] = []

        def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
            del kwargs
            popen_calls.append(command)
            return ffmpeg_process if len(popen_calls) == 1 else fpcalc_process

        captured_request: dict[str, object] = {}

        def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
            captured_request["request"] = request
            captured_request["timeout"] = timeout
            return _FakeResponse(lookup_payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "Side Á.flac"
            source.touch()
            provider = AcoustIDRecognitionProvider(api_key="client-secret")
            runtime = _runtime(root, "fpcalc")
            with mock.patch(
                "groove_serpent.recognition._discover_fingerprint_runtime",
                return_value=(_runtime_status(runtime), runtime),
            ), mock.patch(
                "groove_serpent.recognition.subprocess.Popen", side_effect=fake_popen
            ), mock.patch(
                "groove_serpent.recognition.urllib.request.urlopen",
                side_effect=fake_urlopen,
            ):
                matches = provider.identify_track(
                    source, 0, 44_100 * 200, sample_rate=44_100
                )

        self.assertEqual(matches[0].title, "Recognized Song")
        self.assertTrue(
            any(
                item.startswith("atrim=start_sample=352800:end_sample=5644800,")
                for item in popen_calls[0]
            )
        )
        self.assertEqual(popen_calls[0][-2:], ["wav", "pipe:1"])
        self.assertIn("-nostdin", popen_calls[0])
        self.assertEqual(
            popen_calls[1],
            [runtime.fpcalc.path, "-json", "-length", "120", "-"],
        )
        request = captured_request["request"]
        self.assertEqual(request.full_url, ACOUSTID_LOOKUP_URL)  # type: ignore[attr-defined]
        self.assertEqual(request.get_method(), "POST")  # type: ignore[attr-defined]
        self.assertEqual(  # type: ignore[attr-defined]
            request.get_header("User-agent"),
            __user_agent__,
        )
        fields = urllib.parse.parse_qs(request.data.decode("ascii"))  # type: ignore[attr-defined]
        self.assertEqual(fields["client"], ["client-secret"])
        self.assertEqual(fields["duration"], ["120"])
        self.assertEqual(fields["fingerprint"], ["encoded-fingerprint"])
        self.assertEqual(fields["meta"], ["recordings+releasegroups+releases"])
        self.assertTrue(ffmpeg_process.waited)
        self.assertTrue(fpcalc_process.waited)

    def test_fingerprint_uses_snapshot_during_live_swap_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.flac"
            original = b"original recognition audio" * 64
            source.write_bytes(original)
            fingerprint_paths: list[Path] = []
            provider = AcoustIDRecognitionProvider(api_key="key")

            def fingerprint(
                path: Path,
                excerpt_start: int,
                excerpt_end: int,
                sample_rate: int,
                *,
                runtime: _FingerprintRuntime,
                source_speed_factor: float = 1.0,
            ) -> dict[str, object]:
                del excerpt_start, excerpt_end, sample_rate, runtime
                self.assertEqual(source_speed_factor, 1.0)
                fingerprint_paths.append(path)
                source.write_bytes(b"temporary replacement")
                try:
                    self.assertEqual(path.read_bytes(), original)
                    return {"fingerprint": "encoded", "duration": 10}
                finally:
                    source.write_bytes(original)

            runtime = _runtime(Path(temp_dir), "ffmpeg-chromaprint")
            ready = RecognitionReadiness(
                "acoustid",
                True,
                True,
                "ready",
                fingerprint_backend="ffmpeg-chromaprint",
            )
            with (
                mock.patch.object(provider, "readiness", return_value=ready),
                mock.patch(
                    "groove_serpent.recognition._discover_fingerprint_runtime",
                    return_value=(_runtime_status(runtime), runtime),
                ),
                mock.patch.object(provider, "_fingerprint", side_effect=fingerprint),
                mock.patch.object(
                    provider,
                    "_lookup",
                    return_value={"status": "ok", "results": []},
                ),
                self.assertRaisesRegex(
                    ProjectValidationError,
                    "Recognition source audio changed",
                ),
            ):
                provider.identify_track(source, 0, 44_100 * 20, 44_100)

            self.assertEqual(len(fingerprint_paths), 1)
            self.assertNotEqual(fingerprint_paths[0], source)
            self.assertEqual(source.read_bytes(), original)

    def test_fingerprint_timeout_terminates_and_reaps_both_children(self) -> None:
        ffmpeg_process = _FakeProcess()
        fpcalc_process = _FakeProcess(wait_timeout=True)
        provider = AcoustIDRecognitionProvider(api_key="key")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.flac"
            source.touch()
            runtime = _runtime(root, "fpcalc")
            with mock.patch(
                "groove_serpent.recognition._discover_fingerprint_runtime",
                return_value=(_runtime_status(runtime), runtime),
            ), mock.patch(
                "groove_serpent.recognition.subprocess.Popen",
                side_effect=[ffmpeg_process, fpcalc_process],
            ):
                with self.assertRaisesRegex(RecognitionError, "timed out"):
                    provider.identify_track(source, 0, 44_100 * 60, 44_100)
        self.assertTrue(ffmpeg_process.waited)
        self.assertTrue(fpcalc_process.terminated)
        self.assertTrue(fpcalc_process.waited)

    def test_lookup_rejects_oversized_and_error_json(self) -> None:
        provider = AcoustIDRecognitionProvider(api_key="key", max_response_bytes=20)
        with mock.patch(
            "groove_serpent.recognition.urllib.request.urlopen",
            return_value=_FakeResponse({"status": "ok", "padding": "x" * 100}),
        ):
            with self.assertRaisesRegex(RecognitionError, "large response"):
                provider._lookup(fingerprint="fp", duration=10)

        provider = AcoustIDRecognitionProvider(api_key="key")
        with mock.patch(
            "groove_serpent.recognition.urllib.request.urlopen",
            return_value=_FakeResponse(
                {"status": "error", "error": {"code": 4, "message": "bad key"}}
            ),
        ):
            with self.assertRaisesRegex(RecognitionError, "bad key"):
                provider._lookup(fingerprint="fp", duration=10)

    def test_rate_limit_never_exceeds_three_starts_per_second(self) -> None:
        provider = AcoustIDRecognitionProvider(api_key="key")
        clock = [10.0]
        sleeps: list[float] = []

        def monotonic() -> float:
            return clock[0]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        with mock.patch(
            "groove_serpent.recognition.time.monotonic", side_effect=monotonic
        ), mock.patch(
            "groove_serpent.recognition.time.sleep", side_effect=sleep
        ), mock.patch(
            "groove_serpent.recognition._acoustid_last_request_started", 0.0
        ):
            provider._wait_for_request_slot()
            provider._wait_for_request_slot()
            provider._wait_for_request_slot()
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(value >= 1.0 / 3.0 - 1e-9 for value in sleeps))


if __name__ == "__main__":
    unittest.main()
