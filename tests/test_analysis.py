from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from groove_serpent.analysis import (
    _first_sustained,
    analyze_audio,
    build_tracks,
    detect_candidates,
    estimate_thresholds,
    find_music_bounds,
    select_boundaries,
    smooth_envelope,
)
from groove_serpent.errors import ProjectValidationError, UnsupportedCaptureError
from groove_serpent.models import AnalysisSettings, AudioSource, BoundaryCandidate
from groove_serpent.tracklist import TrackSeed


def _candidate(cut_seconds: float, score: float, sample_rate: int = 100) -> BoundaryCandidate:
    return BoundaryCandidate(
        start_seconds=cut_seconds - 0.5,
        end_seconds=cut_seconds + 0.5,
        cut_seconds=cut_seconds,
        cut_sample=round(cut_seconds * sample_rate),
        duration_seconds=1.0,
        minimum_db=-60.0,
        mean_db=-55.0,
        contrast_db=20.0,
        score=score,
    )


class AnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = 0.05
        # 1s lead, 4s music, 1.2s gap, 4s music, 1.4s gap, 4s music, 1s tail.
        values = (
            [-58.0] * 20
            + [-16.0] * 80
            + [-56.0] * 24
            + [-18.0] * 80
            + [-57.0] * 28
            + [-17.0] * 80
            + [-59.0] * 20
        )
        # Add brief clicks in both gaps. The interruption bridging should keep each gap whole.
        values[108] = -8.0
        values[216] = -9.0
        self.envelope = smooth_envelope(values, 3)
        self.settings = AnalysisSettings(
            min_gap_seconds=0.5,
            min_track_seconds=2.0,
            smoothing_windows=3,
        )

    def test_threshold_and_candidates(self) -> None:
        noise, threshold, active = estimate_thresholds(self.envelope, 6.0)
        self.assertLess(noise, -50.0)
        self.assertLess(threshold, -45.0)
        self.assertGreater(active, threshold)

        bounds = find_music_bounds(
            self.envelope,
            window_seconds=self.window,
            active_threshold_db=active,
            active_run_seconds=0.4,
            lead_in_seconds=0.2,
            tail_seconds=0.3,
            duration_seconds=len(self.envelope) * self.window,
        )
        self.assertIsNotNone(bounds)
        assert bounds is not None
        start, end = bounds
        candidates = detect_candidates(
            self.envelope,
            source_sample_rate=44_100,
            window_seconds=self.window,
            music_start_seconds=start,
            music_end_seconds=end,
            silence_threshold_db=threshold,
            settings=self.settings,
        )
        self.assertEqual(len(candidates), 2)
        self.assertAlmostEqual(candidates[0].cut_seconds, 5.6, delta=0.3)
        self.assertAlmostEqual(candidates[1].cut_seconds, 10.8, delta=0.3)

    def test_exact_selection_returns_requested_count(self) -> None:
        noise, threshold, active = estimate_thresholds(self.envelope, 6.0)
        bounds = find_music_bounds(
            self.envelope,
            window_seconds=self.window,
            active_threshold_db=active,
            active_run_seconds=0.4,
            lead_in_seconds=0.2,
            tail_seconds=0.3,
            duration_seconds=len(self.envelope) * self.window,
        )
        self.assertIsNotNone(bounds)
        assert bounds is not None
        start, end = bounds
        candidates = detect_candidates(
            self.envelope,
            source_sample_rate=44_100,
            window_seconds=self.window,
            music_start_seconds=start,
            music_end_seconds=end,
            silence_threshold_db=threshold,
            settings=self.settings,
        )
        selected = select_boundaries(
            candidates,
            music_start=start,
            music_end=end,
            sample_rate=44_100,
            settings=self.settings,
            expected_track_count=3,
            expected_durations=[4.0, 4.0, 4.0],
        )
        self.assertEqual(len(selected), 2)
        self.assertTrue(all(item.selected for item in selected))

    def test_quiet_edges_use_the_lower_boundary_threshold(self) -> None:
        envelope = np.asarray(
            [-60.0] * 20 + [-38.0] * 20 + [-15.0] * 40 + [-38.0] * 20 + [-60.0] * 20,
            dtype=np.float64,
        )
        bounds = find_music_bounds(
            envelope,
            window_seconds=0.05,
            active_threshold_db=-25.0,
            active_run_seconds=0.4,
            lead_in_seconds=0.2,
            tail_seconds=0.3,
            duration_seconds=6.0,
            boundary_threshold_db=-45.0,
        )
        self.assertIsNotNone(bounds)
        assert bounds is not None
        self.assertAlmostEqual(bounds[0], 0.8, places=3)
        self.assertAlmostEqual(bounds[1], 5.3, places=3)

    def test_virtual_boundaries_fill_missing_gap(self) -> None:
        selected = select_boundaries(
            [],
            music_start=0.0,
            music_end=12.0,
            sample_rate=48_000,
            settings=AnalysisSettings(min_track_seconds=1.0),
            expected_track_count=3,
            expected_durations=[4.0, 4.0, 4.0],
        )
        self.assertEqual(len(selected), 2)
        self.assertAlmostEqual(selected[0].cut_seconds, 4.0, places=4)
        self.assertAlmostEqual(selected[1].cut_seconds, 8.0, places=4)
        self.assertTrue(all(item.score < 0.1 for item in selected))

    def test_side_aware_duration_alignment_uses_side_gap_edges(self) -> None:
        side_gap = BoundaryCandidate(
            start_seconds=100.0,
            end_seconds=130.0,
            cut_seconds=115.0,
            cut_sample=11_500,
            duration_seconds=30.0,
            minimum_db=-60.0,
            mean_db=-55.0,
            contrast_db=20.0,
            score=0.95,
        )
        selected = select_boundaries(
            [
                _candidate(40.5, 0.8),
                _candidate(55.0, 0.95),
                side_gap,
                _candidate(145.0, 0.95),
                _candidate(160.5, 0.8),
            ],
            music_start=0.0,
            music_end=230.0,
            sample_rate=100,
            settings=AnalysisSettings(min_track_seconds=20.0),
            expected_track_count=4,
            expected_durations=[40.0, 60.0, 30.0, 70.0],
            expected_sides=["A", "A", "B", "B"],
        )

        self.assertEqual([item.cut_seconds for item in selected], [40.5, 115.0, 160.5])
        self.assertEqual([item.cut_sample for item in selected], [4_050, 11_500, 16_050])

    def test_automatic_selection_maximizes_count_before_score(self) -> None:
        selected = select_boundaries(
            [_candidate(60.0, 0.8), _candidate(85.0, 0.9), _candidate(110.0, 0.8)],
            music_start=0.0,
            music_end=140.0,
            sample_rate=100,
            settings=AnalysisSettings(
                min_track_seconds=30.0,
                auto_boundary_score=0.5,
            ),
            expected_track_count=None,
        )
        self.assertEqual([item.cut_seconds for item in selected], [60.0, 110.0])
        self.assertTrue(all(item.selected for item in selected))

    def test_exact_count_infeasible_fallback_keeps_real_candidate(self) -> None:
        real = _candidate(35.0, 0.9)
        selected = select_boundaries(
            [real],
            music_start=0.0,
            music_end=70.0,
            sample_rate=100,
            settings=AnalysisSettings(min_track_seconds=30.0),
            expected_track_count=4,
            expected_durations=None,
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].cut_seconds, real.cut_seconds)
        self.assertEqual(selected[0].duration_seconds, real.duration_seconds)
        self.assertGreater(selected[0].score, 0.08)

    def test_first_sustained_handles_runs_longer_than_int16(self) -> None:
        mask = np.concatenate([np.zeros(7, dtype=np.bool_), np.ones(40_000, dtype=np.bool_)])
        self.assertEqual(_first_sustained(mask, 32_768), 7)

    def test_silent_envelope_does_not_create_midpoint_boundary(self) -> None:
        envelope = np.full(400, -120.0, dtype=np.float64)
        _, _, active = estimate_thresholds(envelope, 6.0)
        self.assertIsNone(
            find_music_bounds(
                envelope,
                window_seconds=0.05,
                active_threshold_db=active,
                active_run_seconds=0.45,
                lead_in_seconds=0.2,
                tail_seconds=0.35,
                duration_seconds=20.0,
            )
        )

        source = AudioSource(
            path="silent.wav",
            filename="silent.wav",
            size_bytes=1,
            modified_ns=0,
            duration_seconds=20.0,
            sample_rate=44_100,
            channels=2,
            codec_name="pcm_s16le",
            bits_per_raw_sample=16,
            sample_count=882_000,
        )
        with tempfile.TemporaryDirectory() as directory_value:
            source_path = Path(directory_value) / "silent.wav"
            source_path.write_bytes(b"x")
            with (
                patch("groove_serpent.analysis.probe_audio", return_value=source),
                patch(
                    "groove_serpent.analysis.decode_rms_envelope",
                    return_value=(envelope, 0.05),
                ),
            ):
                project = analyze_audio(
                    source_path,
                    stored_source_path="silent.wav",
                    settings=AnalysisSettings(),
                    expected_track_count=3,
                )

        self.assertEqual(project.analysis.candidates, [])
        self.assertEqual(len(project.tracks), 1)
        self.assertEqual(project.tracks[0].start_sample, 0)
        self.assertEqual(project.tracks[0].end_sample, 882_000)
        self.assertEqual(project.source.path, "silent.wav")
        self.assertEqual(project.source.filename, "silent.wav")
        self.assertEqual(project.source.sha256, hashlib.sha256(b"x").hexdigest())

    def test_retained_edges_do_not_stretch_duration_targets(self) -> None:
        envelope = np.asarray(
            [-60.0] * 200 + [-10.0] * 1_600 + [-60.0] * 200,
            dtype=np.float64,
        )
        source = AudioSource(
            path="conservative.wav",
            filename="conservative.wav",
            size_bytes=1,
            modified_ns=0,
            duration_seconds=100.0,
            sample_rate=44_100,
            channels=2,
            codec_name="pcm_s16le",
            bits_per_raw_sample=16,
            sample_count=4_410_000,
        )
        with tempfile.TemporaryDirectory() as directory_value:
            source_path = Path(directory_value) / "conservative.wav"
            source_path.write_bytes(b"x")
            with (
                patch("groove_serpent.analysis.probe_audio", return_value=source),
                patch(
                    "groove_serpent.analysis.decode_rms_envelope",
                    return_value=(envelope, 0.05),
                ),
            ):
                project = analyze_audio(
                    source_path,
                    stored_source_path="conservative.wav",
                    settings=AnalysisSettings(
                        lead_in_seconds=8.0,
                        tail_seconds=20.0,
                    ),
                    expected_track_count=2,
                    track_seeds=[
                        TrackSeed(title="One", duration_seconds=40.0),
                        TrackSeed(title="Two", duration_seconds=40.0),
                    ],
                )

        self.assertAlmostEqual(project.analysis.music_start_seconds, 2.0, delta=0.2)
        self.assertEqual(project.analysis.music_end_seconds, 100.0)
        self.assertAlmostEqual(project.tracks[0].end_seconds, 50.0, delta=0.2)

    def test_build_tracks_ignores_boundaries_when_no_interior_sample_exists(self) -> None:
        tracks = build_tracks(
            selected=[_candidate(0.0, 0.9, sample_rate=10)],
            music_start=0.0,
            music_end=0.1,
            sample_rate=10,
            track_seeds=None,
            metadata={},
        )
        self.assertEqual(len(tracks), 1)
        self.assertEqual((tracks[0].start_sample, tracks[0].end_sample), (0, 1))

    def test_build_tracks_preserves_per_track_vinyl_sides(self) -> None:
        tracks = build_tracks(
            selected=[_candidate(5.0, 0.9, sample_rate=10)],
            music_start=0.0,
            music_end=10.0,
            sample_rate=10,
            track_seeds=[
                TrackSeed(title="Side A song", side="A"),
                TrackSeed(title="Side B song", side="B"),
            ],
            metadata={"artist": "Example Artist"},
        )
        self.assertEqual([track.side for track in tracks], ["A", "B"])

    def test_invalid_expected_count_is_rejected_before_decoding(self) -> None:
        with (
            patch("groove_serpent.analysis.probe_audio") as probe,
            patch("groove_serpent.analysis.decode_rms_envelope") as decode,
        ):
            for invalid in (0, 1_001, 10**10_000):
                with (
                    self.subTest(invalid=invalid),
                    self.assertRaisesRegex(ValueError, "at least 1"),
                ):
                    analyze_audio(
                        Path("silent.wav"),
                        stored_source_path="silent.wav",
                        settings=AnalysisSettings(),
                        expected_track_count=invalid,
                    )
        probe.assert_not_called()
        decode.assert_not_called()

    def test_unsupported_capture_is_rejected_before_decode(self) -> None:
        source = AudioSource(
            path="lossy.m4a",
            filename="lossy.m4a",
            size_bytes=100,
            modified_ns=1,
            duration_seconds=10.0,
            sample_rate=44_100,
            channels=2,
            codec_name="aac",
            bits_per_raw_sample=16,
            sample_count=441_000,
        )
        with tempfile.TemporaryDirectory() as directory_value:
            source_path = Path(directory_value) / "lossy.m4a"
            source_path.write_bytes(b"not decoded")
            with (
                patch("groove_serpent.analysis.probe_audio", return_value=source),
                patch("groove_serpent.analysis.decode_rms_envelope") as decode,
                self.assertRaisesRegex(
                    UnsupportedCaptureError,
                    "outside lossless-vinyl-capture-v1",
                ),
            ):
                analyze_audio(
                    source_path,
                    stored_source_path=source_path.name,
                    settings=AnalysisSettings(),
                )
        decode.assert_not_called()

    def test_analysis_uses_snapshot_during_live_swap_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            live = directory / "side.flac"
            original = b"original collector audio" * 100
            replacement = b"temporary replacement" * 100
            live.write_bytes(original)
            decoded_paths: list[Path] = []
            probed_hashes: list[str] = []

            def fake_probe(path: Path, stored_path: str | None = None) -> AudioSource:
                del stored_path
                self.assertNotEqual(path, live)
                self.assertEqual(path.read_bytes(), original)
                probed_hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
                return AudioSource(
                    path=str(path),
                    filename=path.name,
                    size_bytes=path.stat().st_size,
                    modified_ns=path.stat().st_mtime_ns,
                    duration_seconds=10.0,
                    sample_rate=44_100,
                    channels=2,
                    codec_name="flac",
                    bits_per_raw_sample=16,
                    sample_count=441_000,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                )

            def fake_decode(
                path: Path,
                *,
                analysis_rate: int,
                window_ms: int,
            ) -> tuple[list[float], float]:
                del analysis_rate, window_ms
                decoded_paths.append(path)
                live.write_bytes(replacement)
                try:
                    self.assertEqual(path.read_bytes(), original)
                    return [-20.0] * 200, 0.05
                finally:
                    live.write_bytes(original)

            with (
                self.assertRaisesRegex(ProjectValidationError, "Source audio changed"),
                patch("groove_serpent.analysis.probe_audio", side_effect=fake_probe),
                patch(
                    "groove_serpent.analysis.decode_rms_envelope",
                    side_effect=fake_decode,
                ),
            ):
                analyze_audio(
                    live,
                    stored_source_path=live.name,
                    settings=AnalysisSettings(min_track_seconds=0.1),
                )

            self.assertEqual(len(decoded_paths), 1)
            self.assertNotEqual(decoded_paths[0], live)
            self.assertEqual(
                probed_hashes,
                [hashlib.sha256(original).hexdigest()],
            )
            self.assertEqual(
                hashlib.sha256(live.read_bytes()).hexdigest(),
                hashlib.sha256(original).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
