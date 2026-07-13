from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

from groove_serpent.evidence import (
    EVIDENCE_SCHEMA,
    MAX_EVIDENCE_DECODE_BYTES,
    EvidenceCache,
    EvidenceCacheKey,
    EvidenceRequestSuperseded,
    _decode_exact_float,
    _evidence_decode_size,
    analyze_evidence_window,
    evidence_cache_key,
)
from groove_serpent.audio_snapshot import verified_audio_snapshot
from groove_serpent.errors import GrooveSerpentError, ProjectValidationError
from groove_serpent.media import probe_audio


class EvidenceDecodeBudgetTests(unittest.TestCase):
    def test_decode_budget_retains_stereo_thirty_seconds_and_rejects_max_geometry(
        self,
    ) -> None:
        ordinary_frames = 48_000 * 30
        self.assertEqual(
            _evidence_decode_size(ordinary_frames, 2),
            ordinary_frames * 2 * 4,
        )

        maximum_geometry_frames = 768_000 * 30
        self.assertGreater(
            maximum_geometry_frames * 64 * 4,
            MAX_EVIDENCE_DECODE_BYTES,
        )
        with self.assertRaisesRegex(
            ProjectValidationError,
            "decoded PCM.*local safety limit",
        ):
            _evidence_decode_size(maximum_geometry_frames, 64)

        with patch(
            "groove_serpent.evidence.find_tool",
            side_effect=AssertionError("oversized evidence launched FFmpeg"),
        ) as finder, self.assertRaisesRegex(
            ProjectValidationError,
            "decoded PCM.*local safety limit",
        ):
            _decode_exact_float(
                Path("unused.flac"),
                start_sample=0,
                end_sample=maximum_geometry_frames,
                sample_rate=768_000,
                channels=64,
            )
        finder.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class EvidenceTests(unittest.TestCase):
    sample_rate = 48_000
    frame_count = 48_000

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        time = np.arange(self.frame_count, dtype=np.float64) / self.sample_rate
        stereo = np.column_stack(
            (
                0.25 * np.sin(2 * np.pi * 440 * time),
                0.20 * np.sin(2 * np.pi * 660 * time),
            )
        ).astype("<f4")
        stereo[24_000] = (0.95, -0.95)
        self.source_path = self.directory / "evidence.flac"
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "f32le",
                "-ar",
                str(self.sample_rate),
                "-ac",
                "2",
                "-i",
                "pipe:0",
                "-c:a",
                "flac",
                "-sample_fmt",
                "s32",
                str(self.source_path),
            ],
            input=stereo.tobytes(),
            check=True,
        )
        self.source = probe_audio(self.source_path, stored_path=self.source_path.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_waveform_spectrogram_and_transients_share_exact_selection(self) -> None:
        payload = analyze_evidence_window(
            self.source_path,
            self.source,
            start_sample=12_000,
            end_sample=36_000,
            focus_sample=24_000,
            waveform_points=120,
            spectrogram_time_bins=40,
            spectrogram_frequency_bins=32,
        )
        self.assertEqual(payload["schema"], EVIDENCE_SCHEMA)
        self.assertEqual(payload["selection"]["sample_count"], 24_000)
        self.assertEqual(payload["selection"]["focus_sample"], 24_000)
        self.assertEqual(payload["waveform"]["point_count"], 120)
        self.assertEqual(len(payload["waveform"]["channels"]), 2)
        self.assertEqual(len(payload["spectrogram"]["frequency_hz"]), 32)
        self.assertEqual(len(payload["spectrogram"]["dbfs"]), 32)
        self.assertTrue(
            any(abs(item["sample"] - 24_000) < 8 for item in payload["transients"])
        )
        self.assertTrue(payload["focus_evidence"]["transient_within_30_ms"])

    def test_borrowed_snapshot_evidence_uses_only_cheap_lease_checks(self) -> None:
        snapshot = verified_audio_snapshot(
            self.source_path,
            expected_sha256=self.source.sha256,
            expected_size_bytes=self.source.size_bytes,
            workspace=self.directory / "snapshots",
        )
        try:
            with patch(
                "groove_serpent.audio_snapshot.assert_file_receipt",
                side_effect=AssertionError("evidence must not full-hash either file"),
            ):
                payload = analyze_evidence_window(
                    self.source_path,
                    self.source,
                    start_sample=12_000,
                    end_sample=36_000,
                    focus_sample=24_000,
                    source_snapshot=snapshot,
                )
                self.assertEqual(payload["selection"]["focus_sample"], 24_000)
        finally:
            snapshot.close()

    def test_evidence_cache_is_bounded_deterministic_and_copy_safe(self) -> None:
        cache = EvidenceCache(maximum_entries=2, maximum_bytes=100_000)
        keys = [
            EvidenceCacheKey(
                source_sha256="a" * 64,
                source_filename="side.flac",
                source_sample_rate=48_000,
                source_channels=2,
                source_sample_count=100,
                start_sample=index,
                end_sample=index + 10,
                focus_sample=index + 5,
                waveform_points=20,
                spectrogram_time_bins=8,
                spectrogram_frequency_bins=16,
            )
            for index in range(3)
        ]
        cache.put(keys[0], {"value": [0]})
        cache.put(keys[1], {"value": [1]})
        returned = cache.get(keys[0])
        assert returned is not None
        returned["value"].append(99)
        cache.put(keys[2], {"value": [2]})

        self.assertIsNone(cache.get(keys[1]))
        self.assertEqual(cache.get(keys[0]), {"value": [0]})
        self.assertEqual(cache.get(keys[2]), {"value": [2]})
        self.assertEqual(len(cache), 2)

    def test_evidence_cache_key_binds_emitted_source_geometry(self) -> None:
        parameters = {
            "start_sample": 100,
            "end_sample": 900,
            "focus_sample": 500,
        }
        baseline = evidence_cache_key(self.source, **parameters)
        variants = (
            replace(self.source, filename="renamed.flac"),
            replace(self.source, sample_rate=self.source.sample_rate + 1),
            replace(self.source, channels=1),
            replace(self.source, sample_count=self.frame_count + 1),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(
                    evidence_cache_key(variant, **parameters),
                    baseline,
                )

    def test_cancelled_decoder_terminates_and_reaps_ffmpeg(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.terminated = False
                self.killed = False

            def poll(self) -> int | None:
                return -15 if self.terminated or self.killed else None

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                if self.terminated or self.killed:
                    return -15
                raise subprocess.TimeoutExpired("ffmpeg", 0.025)

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True

        process = FakeProcess()
        checks = iter((False, True))
        with patch(
            "groove_serpent.evidence.subprocess.Popen",
            return_value=process,
        ), self.assertRaises(EvidenceRequestSuperseded):
            _decode_exact_float(
                self.source_path,
                start_sample=0,
                end_sample=100,
                sample_rate=self.sample_rate,
                channels=2,
                cancelled=lambda: next(checks),
            )
        self.assertTrue(process.terminated)

    def test_strict_bounds_and_complexity_limits(self) -> None:
        cases = [
            {"start_sample": True, "end_sample": 10},
            {"start_sample": 10, "end_sample": 10},
            {"start_sample": 0, "end_sample": self.frame_count + 1},
            {"start_sample": 0, "end_sample": 100, "focus_sample": 100},
            {"start_sample": 0, "end_sample": 100, "waveform_points": 2},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ProjectValidationError):
                analyze_evidence_window(self.source_path, self.source, **kwargs)

    def test_decoder_refuses_non_exact_pcm_length(self) -> None:
        with patch(
            "groove_serpent.evidence.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout=b"\0" * 7, stderr=b""),
        ):
            with self.assertRaises(GrooveSerpentError):
                analyze_evidence_window(
                    self.source_path,
                    self.source,
                    start_sample=0,
                    end_sample=100,
                )

    def test_evidence_decode_cannot_follow_live_swap_and_restore(self) -> None:
        original = self.source_path.read_bytes()
        decoded_paths: list[Path] = []

        def swap_while_decoding(
            path: Path,
            *,
            start_sample: int,
            end_sample: int,
            sample_rate: int,
            channels: int,
        ) -> np.ndarray:
            decoded_paths.append(path)
            self.source_path.write_bytes(b"temporary replacement")
            try:
                return _decode_exact_float(
                    path,
                    start_sample=start_sample,
                    end_sample=end_sample,
                    sample_rate=sample_rate,
                    channels=channels,
                )
            finally:
                self.source_path.write_bytes(original)

        with patch(
            "groove_serpent.evidence._decode_exact_float",
            side_effect=swap_while_decoding,
        ), self.assertRaisesRegex(ProjectValidationError, "Source audio changed"):
            analyze_evidence_window(
                self.source_path,
                self.source,
                start_sample=12_000,
                end_sample=36_000,
                focus_sample=24_000,
            )

        self.assertEqual(len(decoded_paths), 1)
        self.assertNotEqual(decoded_paths[0], self.source_path)
        self.assertEqual(self.source_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
