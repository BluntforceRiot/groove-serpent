from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from groove_serpent import __user_agent__
from groove_serpent.errors import ProjectValidationError
from groove_serpent.recognition import (
    ACOUSTID_LOOKUP_URL,
    AcoustIDRecognitionProvider,
    NoRecognitionProvider,
    RecognitionError,
    RecognitionMatch,
    RecognitionProvider,
    RecognitionReadiness,
    _excerpt_sample_bounds,
    _find_fpcalc,
    _parse_matches,
)


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        communicate_timeout: bool = False,
    ) -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self._communicate_timeout = communicate_timeout
        self.terminated = False
        self.killed = False
        self.waited = False

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        del timeout
        if self._communicate_timeout and not self.terminated:
            raise subprocess.TimeoutExpired("fpcalc", 1)
        return self.stdout.read(), self.stderr.read()

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.waited = True
        return self.returncode

    def poll(self) -> int | None:
        return None if self._communicate_timeout and not self.terminated else self.returncode

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
            "groove_serpent.recognition.shutil.which", return_value=None
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
            "groove_serpent.recognition.shutil.which",
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
            executable = Path(temp_dir) / "fp calc.exe"
            executable.touch()
            with mock.patch.dict(
                os.environ, {"GROOVE_SERPENT_FPCALC": str(executable)}, clear=True
            ), mock.patch(
                "groove_serpent.recognition.shutil.which", return_value="path-fpcalc"
            ):
                self.assertEqual(_find_fpcalc(), str(executable.resolve()))


class RecognitionPipelineTests(unittest.TestCase):
    def test_excerpt_skips_lead_and_is_capped_at_120_seconds(self) -> None:
        start, end = _excerpt_sample_bounds(44_100, 44_100 * 300, 44_100)
        self.assertEqual(start, 44_100 * 9)
        self.assertEqual(end - start, 44_100 * 120)

        short_start, short_end = _excerpt_sample_bounds(0, 44_100 * 10, 44_100)
        self.assertEqual((short_start, short_end), (0, 44_100 * 10))

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
            source = Path(temp_dir) / "Side Á.flac"
            source.touch()
            provider = AcoustIDRecognitionProvider(api_key="client-secret")
            with mock.patch(
                "groove_serpent.recognition._find_fpcalc", return_value="fpcalc"
            ), mock.patch(
                "groove_serpent.recognition.shutil.which", return_value="ffmpeg"
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
        self.assertEqual(popen_calls[1], ["fpcalc", "-json", "-length", "120", "-"])
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
                ffmpeg: str,
                fpcalc: str,
            ) -> dict[str, object]:
                del excerpt_start, excerpt_end, sample_rate, ffmpeg, fpcalc
                fingerprint_paths.append(path)
                source.write_bytes(b"temporary replacement")
                try:
                    self.assertEqual(path.read_bytes(), original)
                    return {"fingerprint": "encoded", "duration": 10}
                finally:
                    source.write_bytes(original)

            ready = RecognitionReadiness("acoustid", True, True, "ready")
            with (
                mock.patch.object(provider, "readiness", return_value=ready),
                mock.patch(
                    "groove_serpent.recognition._find_fpcalc",
                    return_value="fpcalc",
                ),
                mock.patch(
                    "groove_serpent.recognition.shutil.which",
                    return_value="ffmpeg",
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
        fpcalc_process = _FakeProcess(communicate_timeout=True)
        provider = AcoustIDRecognitionProvider(api_key="key")
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.flac"
            source.touch()
            with mock.patch(
                "groove_serpent.recognition._find_fpcalc", return_value="fpcalc"
            ), mock.patch(
                "groove_serpent.recognition.shutil.which", return_value="ffmpeg"
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
