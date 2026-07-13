from __future__ import annotations

import unittest

import numpy as np

from groove_serpent.restoration import (
    ClickInterval,
    detect_clipped_runs,
    detect_impulsive_clicks,
    group_click_candidates,
    repair_click_intervals,
)


class RestorationTests(unittest.TestCase):
    def _music_like_stereo(self) -> np.ndarray:
        sample_rate = 8_000
        frame_count = 4_096
        time = np.arange(frame_count, dtype=np.float64) / sample_rate
        music = (
            0.25 * np.sin(2.0 * np.pi * 220.0 * time)
            + 0.12 * np.sin(2.0 * np.pi * 443.0 * time + 0.3)
        )
        music *= 0.7 + 0.3 * np.sin(2.0 * np.pi * 2.0 * time) ** 2

        # A broad, decaying percussion-like onset is deliberately much louder
        # than its surroundings but is not an isolated out-and-back click.
        percussion = np.zeros(frame_count, dtype=np.float64)
        offset = np.arange(180, dtype=np.float64)
        percussion[900:1_080] = (
            0.55
            * np.exp(-offset / 35.0)
            * np.sin(2.0 * np.pi * 90.0 * offset / sample_rate)
        )
        music += percussion
        return np.column_stack(
            (music, 0.9 * music + 0.03 * np.sin(2.0 * np.pi * 331.0 * time))
        )

    def test_grouping_returns_exact_half_open_stereo_intervals(self) -> None:
        mask = np.zeros((20, 2), dtype=np.bool_)
        mask[4, 0] = True
        mask[9:12, 1] = True
        scores = np.zeros_like(mask, dtype=np.float64)
        scores[4, 0] = 0.75
        scores[9:12, 1] = [0.4, 0.9, 0.6]

        intervals = group_click_candidates(mask, scores=scores)

        self.assertEqual(
            [(item.start_sample, item.end_sample) for item in intervals],
            [(4, 5), (9, 12)],
        )
        self.assertEqual(intervals[1].peak_sample, 10)
        self.assertEqual(intervals[1].channels, (1,))
        self.assertAlmostEqual(intervals[1].confidence, 0.9)

    def test_grouping_bridges_only_when_explicitly_requested(self) -> None:
        mask = np.zeros(12, dtype=np.bool_)
        mask[[3, 5]] = True

        exact = group_click_candidates(mask)
        bridged = group_click_candidates(mask, max_gap_samples=1)

        self.assertEqual(
            [(item.start_sample, item.end_sample) for item in exact],
            [(3, 4), (5, 6)],
        )
        self.assertEqual(
            [(item.start_sample, item.end_sample) for item in bridged],
            [(3, 6)],
        )

    def test_detector_finds_injected_clicks_but_not_music_transient(self) -> None:
        audio = self._music_like_stereo()
        damaged = audio.copy()
        damaged[1_800] += [0.9, 0.75]
        damaged[2_600:2_603, 0] += [0.85, -0.7, 0.5]

        intervals = detect_impulsive_clicks(damaged)

        self.assertEqual(
            [(item.start_sample, item.end_sample) for item in intervals],
            [(1_800, 1_801), (2_600, 2_603)],
        )
        self.assertEqual(intervals[0].channels, (0, 1))
        self.assertEqual(intervals[1].channels, (0,))
        self.assertTrue(all(item.confidence > 0.9 for item in intervals))
        self.assertFalse(any(item.start_sample <= 900 < item.end_sample for item in intervals))

    def test_detector_rejects_a_broad_impulsive_region_instead_of_truncating_it(self) -> None:
        audio = np.zeros(256, dtype=np.float64)
        audio[100:120:2] = 1.0
        audio[101:120:2] = -1.0

        intervals = detect_impulsive_clicks(
            audio,
            local_window_samples=65,
            max_click_samples=8,
        )

        self.assertEqual(intervals, [])

    def test_public_numeric_parameters_reject_extreme_integers_cleanly(self) -> None:
        audio = np.zeros(256, dtype=np.float64)
        enormous = 10**10_000

        with self.assertRaisesRegex(ValueError, "threshold_sigma"):
            detect_impulsive_clicks(audio, threshold_sigma=enormous)
        with self.assertRaisesRegex(ValueError, "threshold_ratio"):
            detect_clipped_runs(audio, threshold_ratio=enormous)
        with self.assertRaisesRegex(ValueError, "confidence"):
            ClickInterval(1, 2, 1, enormous, (0,))

    def test_clipped_run_detector_finds_short_plateau(self) -> None:
        audio = np.zeros((512, 2), dtype=np.int16)
        audio[200:247, 0] = np.iinfo(np.int16).min
        audio[220:240, 1] = np.iinfo(np.int16).min

        intervals = detect_clipped_runs(audio)

        self.assertEqual(len(intervals), 2)
        self.assertEqual(
            [
                (item.start_sample, item.end_sample, item.channels)
                for item in intervals
            ],
            [(200, 247, (0,)), (220, 240, (1,))],
        )

    def test_clipped_run_detector_merges_only_matching_channel_windows(self) -> None:
        audio = np.zeros((512, 2), dtype=np.int16)
        audio[200:230] = np.iinfo(np.int16).min

        intervals = detect_clipped_runs(audio)

        self.assertEqual(len(intervals), 1)
        self.assertEqual(
            (
                intervals[0].start_sample,
                intervals[0].end_sample,
                intervals[0].channels,
            ),
            (200, 230, (0, 1)),
        )

    def test_bidirectional_repair_beats_linear_fill_for_periodic_detail(self) -> None:
        sample_rate = 8_000
        time = np.arange(4_096, dtype=np.float64) / sample_rate
        pristine = np.column_stack(
            (
                0.25 * np.sin(2.0 * np.pi * 220.0 * time)
                + 0.12 * np.sin(2.0 * np.pi * 443.0 * time + 0.3)
                + 0.08 * np.sin(2.0 * np.pi * 701.0 * time + 0.9),
                0.2 * np.sin(2.0 * np.pi * 173.0 * time + 0.1)
                + 0.1 * np.sin(2.0 * np.pi * 517.0 * time + 0.6),
            )
        )
        start, end = 1_800, 1_832
        damaged = pristine.copy()
        damaged[start:end] = np.linspace(0.9, -0.9, end - start)[:, np.newaxis]

        repaired = repair_click_intervals(damaged, [(start, end)])
        linear = np.vstack(
            [
                np.linspace(damaged[start - 1, channel], damaged[end, channel], end - start + 2)[
                    1:-1
                ]
                for channel in range(damaged.shape[1])
            ]
        ).T
        repair_error = float(np.sqrt(np.mean((repaired[start:end] - pristine[start:end]) ** 2)))
        linear_error = float(np.sqrt(np.mean((linear - pristine[start:end]) ** 2)))

        self.assertLess(repair_error, linear_error * 0.1)

    def test_repair_changes_only_explicit_intervals_bit_for_bit(self) -> None:
        rng = np.random.default_rng(20260711)
        source = rng.normal(0.0, 0.1, size=(2_048, 2)).astype(np.float32)
        original = source.copy()
        intervals = [
            ClickInterval(400, 403, 401, 0.98, (0, 1)),
            ClickInterval(1_200, 1_201, 1_200, 0.95, (0,)),
        ]

        repaired = repair_click_intervals(source, intervals)
        changed_windows = np.zeros(source.shape[0], dtype=np.bool_)
        for interval in intervals:
            changed_windows[interval.start_sample : interval.end_sample] = True

        self.assertTrue(np.array_equal(source, original))
        self.assertTrue(np.array_equal(repaired[~changed_windows], source[~changed_windows]))
        self.assertFalse(np.array_equal(repaired[changed_windows], source[changed_windows]))
        self.assertTrue(np.array_equal(repaired[1_200:1_201, 1], source[1_200:1_201, 1]))
        self.assertEqual(repaired.dtype, source.dtype)
        self.assertEqual(repaired.shape, source.shape)

    def test_channel_disjoint_intervals_may_overlap_in_time(self) -> None:
        time = np.arange(1_024, dtype=np.float64) / 8_000.0
        source = np.column_stack(
            (
                np.sin(2.0 * np.pi * 220.0 * time),
                np.sin(2.0 * np.pi * 330.0 * time + 0.2),
            )
        )
        damaged = source.copy()
        damaged[400:420, 0] = 1.0
        damaged[410:430, 1] = -1.0
        intervals = [
            ClickInterval(400, 420, 400, 0.9, (0,)),
            ClickInterval(410, 430, 410, 0.9, (1,)),
        ]

        repaired = repair_click_intervals(damaged, intervals)

        allowed = np.zeros(damaged.shape, dtype=np.bool_)
        allowed[400:420, 0] = True
        allowed[410:430, 1] = True
        self.assertTrue(np.array_equal(repaired[~allowed], damaged[~allowed]))
        self.assertFalse(np.array_equal(repaired[allowed], damaged[allowed]))

    def test_mono_integer_audio_preserves_shape_dtype_and_untouched_values(self) -> None:
        time = np.arange(1_024, dtype=np.float64) / 8_000.0
        source = np.rint(10_000.0 * np.sin(2.0 * np.pi * 250.0 * time)).astype(np.int16)
        source[500:503] = [30_000, -30_000, 30_000]

        repaired = repair_click_intervals(source, [(500, 503)])

        self.assertEqual(repaired.shape, source.shape)
        self.assertEqual(repaired.dtype, np.int16)
        self.assertTrue(np.array_equal(repaired[:500], source[:500]))
        self.assertTrue(np.array_equal(repaired[503:], source[503:]))

    def test_repair_rejects_unsafe_or_ambiguous_intervals(self) -> None:
        audio = np.zeros(512, dtype=np.float64)

        with self.assertRaisesRegex(ValueError, "at most 128"):
            repair_click_intervals(audio, [(100, 229)])
        with self.assertRaisesRegex(ValueError, "clean sample on both sides"):
            repair_click_intervals(audio, [(0, 1)])
        with self.assertRaisesRegex(ValueError, "overlap or touch"):
            repair_click_intervals(audio, [(100, 102), (102, 104)])
        with self.assertRaisesRegex(ValueError, "channel outside"):
            repair_click_intervals(
                np.zeros((512, 2), dtype=np.float64),
                [ClickInterval(100, 101, 100, 0.9, (2,))],
            )


if __name__ == "__main__":
    unittest.main()
