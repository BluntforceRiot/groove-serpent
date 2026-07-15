from __future__ import annotations

import json
import unittest
from dataclasses import replace
from typing import Any

from groove_serpent.capture_envelope import (
    CAPTURE_ENVELOPE_SCHEMA,
    CAPTURE_PROFILE_ID,
    MAX_ANALYSIS_WINDOWS,
    MAX_CAPTURE_DURATION_SECONDS,
    MAX_CAPTURE_SIZE_BYTES,
    SUPPORTED_SAMPLE_RATES,
    evaluate_capture_envelope,
    require_supported_capture,
)
from groove_serpent.errors import UnsupportedCaptureError
from groove_serpent.models import AudioSource


def _source(**changes: Any) -> AudioSource:
    source = AudioSource(
        path="side-a.flac",
        filename="side-a.flac",
        size_bytes=800_000_000,
        modified_ns=1,
        duration_seconds=2_400.0,
        sample_rate=44_100,
        channels=2,
        codec_name="flac",
        bits_per_raw_sample=24,
        sample_format="s32",
        sample_count=105_840_000,
        sha256="a" * 64,
    )
    return replace(source, **changes)


class CaptureEnvelopeTests(unittest.TestCase):
    def test_supported_realistic_flac_has_bounded_resource_estimate(self) -> None:
        report = require_supported_capture(_source())

        self.assertTrue(report.supported)
        self.assertEqual(report.schema, CAPTURE_ENVELOPE_SCHEMA)
        self.assertEqual(report.profile_id, CAPTURE_PROFILE_ID)
        self.assertEqual(report.reason_codes, ())
        self.assertEqual(report.decoded_pcm_bytes, 635_040_000)
        self.assertEqual(report.analysis_window_count, 48_000)

    def test_declared_sample_rates_are_supported_at_the_profile_boundary(self) -> None:
        for rate in SUPPORTED_SAMPLE_RATES:
            with self.subTest(rate=rate):
                report = evaluate_capture_envelope(
                    _source(
                        sample_rate=rate,
                        duration_seconds=1.0,
                        sample_count=rate,
                    )
                )
                self.assertTrue(report.supported, report.reason_codes)

    def test_supported_lossless_container_codec_combinations(self) -> None:
        combinations = (
            ("side.flac", "flac", 24),
            ("side.wav", "pcm_s16le", 16),
            ("side.wave", "pcm_s24le", 24),
            ("side.aif", "pcm_s16be", 16),
            ("side.aiff", "pcm_s24be", 24),
        )
        for filename, codec, bits in combinations:
            with self.subTest(filename=filename, codec=codec):
                report = evaluate_capture_envelope(
                    _source(
                        filename=filename,
                        path=filename,
                        codec_name=codec,
                        bits_per_raw_sample=bits,
                    )
                )
                self.assertTrue(report.supported, report.reason_codes)

    def test_lossy_mismatched_float_and_multichannel_inputs_fail_closed(self) -> None:
        cases = (
            (
                _source(filename="side.m4a", codec_name="aac"),
                {"container_not_supported", "codec_container_not_supported"},
            ),
            (
                _source(filename="side.wav", codec_name="flac"),
                {"codec_container_not_supported"},
            ),
            (
                _source(
                    filename="side.wav",
                    codec_name="pcm_f32le",
                    bits_per_raw_sample=32,
                ),
                {"codec_container_not_supported", "bit_depth_not_supported"},
            ),
            (_source(channels=6), {"channel_count_not_supported"}),
        )
        for source, expected in cases:
            with self.subTest(source=source.filename, codec=source.codec_name):
                report = evaluate_capture_envelope(source)
                self.assertFalse(report.supported)
                self.assertTrue(expected.issubset(report.reason_codes))

    def test_unknown_or_inconsistent_geometry_is_not_guessed(self) -> None:
        unknown = evaluate_capture_envelope(_source(bits_per_raw_sample=None, sample_count=None))
        inconsistent = evaluate_capture_envelope(_source(sample_count=10))

        self.assertEqual(
            unknown.reason_codes,
            ("bit_depth_unknown", "sample_count_missing"),
        )
        self.assertIn("sample_count_inconsistent", inconsistent.reason_codes)

    def test_six_hour_192khz_capture_fits_but_one_millisecond_analysis_does_not(self) -> None:
        duration = float(MAX_CAPTURE_DURATION_SECONDS)
        sample_count = 192_000 * MAX_CAPTURE_DURATION_SECONDS
        source = _source(
            duration_seconds=duration,
            sample_rate=192_000,
            sample_count=sample_count,
            size_bytes=MAX_CAPTURE_SIZE_BYTES,
        )

        default_report = evaluate_capture_envelope(source)
        dense_report = evaluate_capture_envelope(source, analysis_window_ms=1)

        self.assertTrue(default_report.supported, default_report.reason_codes)
        self.assertLessEqual(
            default_report.analysis_window_count or 0,
            MAX_ANALYSIS_WINDOWS,
        )
        self.assertFalse(dense_report.supported)
        self.assertIn(
            "analysis_window_count_exceeds_limit",
            dense_report.reason_codes,
        )

    def test_duration_size_rate_and_bit_depth_limits_are_inclusive_and_early(self) -> None:
        source = _source(
            duration_seconds=float(MAX_CAPTURE_DURATION_SECONDS + 1),
            sample_count=44_100 * (MAX_CAPTURE_DURATION_SECONDS + 1),
            size_bytes=MAX_CAPTURE_SIZE_BYTES + 1,
            sample_rate=384_000,
            bits_per_raw_sample=32,
        )
        report = evaluate_capture_envelope(source)

        self.assertFalse(report.supported)
        self.assertIn("duration_exceeds_limit", report.reason_codes)
        self.assertIn("source_size_exceeds_limit", report.reason_codes)
        self.assertIn("sample_rate_not_supported", report.reason_codes)
        self.assertIn("bit_depth_not_supported", report.reason_codes)

    def test_report_json_is_deterministic_and_contains_declared_limits(self) -> None:
        first = evaluate_capture_envelope(_source()).to_dict()
        second = evaluate_capture_envelope(replace(_source())).to_dict()

        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, sort_keys=True, separators=(",", ":")),
            json.dumps(second, sort_keys=True, separators=(",", ":")),
        )
        self.assertEqual(
            first["limits"]["sample_rates"],
            list(SUPPORTED_SAMPLE_RATES),
        )

    def test_require_error_is_actionable_and_does_not_mutate_source(self) -> None:
        source = _source(filename="capture.mp3", codec_name="mp3")
        before = replace(source)

        with self.assertRaisesRegex(
            UnsupportedCaptureError,
            "source was not analyzed or modified",
        ):
            require_supported_capture(source)

        self.assertEqual(source, before)

    def test_adversarial_numeric_metadata_fails_closed_with_json_safe_report(self) -> None:
        enormous = 10**10_000
        report = evaluate_capture_envelope(
            _source(
                filename="folder/side.flac",
                size_bytes=enormous,
                duration_seconds=enormous,
                sample_rate=enormous,
                channels=enormous,
                bits_per_raw_sample=enormous,
                sample_count=enormous,
            ),
            analysis_rate=enormous,
            analysis_window_ms=enormous,
        )

        self.assertFalse(report.supported)
        self.assertIn("filename_invalid", report.reason_codes)
        self.assertIn("duration_invalid", report.reason_codes)
        self.assertIn("sample_count_exceeds_limit", report.reason_codes)
        self.assertIn("analysis_settings_invalid", report.reason_codes)
        self.assertIsNone(report.sample_rate)
        self.assertIsNone(report.sample_count)
        json.dumps(report.to_dict(), allow_nan=False)


if __name__ == "__main__":
    unittest.main()
