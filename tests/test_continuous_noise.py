from __future__ import annotations

import copy
import unittest

import numpy as np

from groove_serpent.continuous_noise import (
    CONTINUOUS_NOISE_ALGORITHM_ID,
    CONTINUOUS_NOISE_DOCUMENT_SCHEMA,
    HUM_PROPOSAL_SCHEMA,
    RUMBLE_PROPOSAL_SCHEMA,
    ContinuousNoiseProposalDocument,
    NoiseAnalysisScope,
    NoiseReferenceRegion,
    analyze_continuous_noise,
    validate_continuous_noise_proposal_document,
)
from groove_serpent.errors import ProjectValidationError
from groove_serpent.publication import canonical_json_sha256


SAMPLE_RATE = 8_000
DURATION_SECONDS = 24
SAMPLE_COUNT = SAMPLE_RATE * DURATION_SECONDS


def _geometry(
    sample_count: int = SAMPLE_COUNT,
) -> tuple[NoiseAnalysisScope, tuple[NoiseReferenceRegion, ...]]:
    scope = NoiseAnalysisScope("side-a", 0, sample_count)
    references = (
        NoiseReferenceRegion("lead-in", "lead_in", 0, 4 * SAMPLE_RATE),
        NoiseReferenceRegion(
            "lead-out",
            "lead_out",
            sample_count - 4 * SAMPLE_RATE,
            sample_count,
        ),
    )
    return scope, references


def _base(seed: int = 137) -> tuple[np.ndarray, np.ndarray]:
    time = np.arange(SAMPLE_COUNT, dtype=np.float64) / SAMPLE_RATE
    noise = np.random.default_rng(seed).normal(0.0, 0.00001, (SAMPLE_COUNT, 2))
    return noise, time


def _hum(frequency: int, *, channels: tuple[int, int] | None = None) -> np.ndarray:
    samples, time = _base(1_000 + frequency)
    channel_frequencies = channels or (frequency, frequency)
    for channel, channel_frequency in enumerate(channel_frequencies):
        samples[:, channel] += (
            0.0020 * np.sin(2.0 * np.pi * channel_frequency * time)
            + 0.0008 * np.sin(2.0 * np.pi * channel_frequency * 2 * time)
            + 0.0004 * np.sin(2.0 * np.pi * channel_frequency * 3 * time)
        )
    program = slice(4 * SAMPLE_RATE, 20 * SAMPLE_RATE)
    music = 0.04 * np.sin(2.0 * np.pi * 440.0 * time[program])
    samples[program, :] += music[:, np.newaxis]
    return samples


def _diffuse_rumble(*, second_channel: bool = True) -> np.ndarray:
    samples, time = _base(900)
    rng = np.random.default_rng(901)
    source = rng.normal(0.0, 1.0, SAMPLE_COUNT)
    frequencies = np.fft.rfftfreq(SAMPLE_COUNT, d=1.0 / SAMPLE_RATE)
    spectrum = np.fft.rfft(source)
    spectrum[(frequencies < 5.0) | (frequencies > 28.0)] = 0.0
    rumble = np.fft.irfft(spectrum, n=SAMPLE_COUNT)
    rumble *= 0.004 / float(np.std(rumble))
    samples[:, 0] += rumble
    if second_channel:
        samples[:, 1] += rumble * 0.93
    else:
        samples[:, 1] += np.random.default_rng(902).normal(
            0.0,
            0.0001,
            SAMPLE_COUNT,
        )
    program = slice(4 * SAMPLE_RATE, 20 * SAMPLE_RATE)
    music = 0.03 * np.sin(2.0 * np.pi * 440.0 * time[program])
    samples[program, :] += music[:, np.newaxis]
    return samples


def _analyze(samples: np.ndarray) -> ContinuousNoiseProposalDocument:
    scope, references = _geometry(samples.shape[0])
    return analyze_continuous_noise(
        samples,
        sample_rate=SAMPLE_RATE,
        scope=scope,
        noise_references=references,
    )


def _rehash(value: dict[str, object]) -> None:
    body = copy.deepcopy(value)
    del body["proposal_body_sha256"]
    value["proposal_body_sha256"] = canonical_json_sha256(body)


class ContinuousNoiseEvidenceTests(unittest.TestCase):
    def test_stable_50_and_60_hz_hum_require_persistent_harmonics(self) -> None:
        for frequency in (50, 60):
            with self.subTest(frequency=frequency):
                document = _analyze(_hum(frequency))
                self.assertEqual(document.hum.status, "proposed")
                self.assertEqual(document.hum.fundamental_hz, frequency)
                self.assertGreater(document.hum.confidence, 0.0)
                self.assertIn(1, document.hum.detected_harmonics)
                self.assertGreaterEqual(len(document.hum.detected_harmonics), 2)
                self.assertTrue(document.hum.requires_review)
                self.assertEqual(document.rumble.status, "abstained")

    def test_unstable_line_and_isolated_bass_tone_abstain(self) -> None:
        samples, time = _base(200)
        window = 2 * SAMPLE_RATE
        for index, start in enumerate(range(0, SAMPLE_COUNT, window)):
            frequency = 50 if index % 2 == 0 else 54
            end = min(start + window, SAMPLE_COUNT)
            samples[start:end, :] += (0.002 * np.sin(2.0 * np.pi * frequency * time[start:end]))[
                :, np.newaxis
            ]
        unstable = _analyze(samples)
        self.assertEqual(unstable.hum.status, "abstained")
        self.assertEqual(
            unstable.hum.reasons,
            ("line_not_persistent_across_references",),
        )

        isolated, time = _base(201)
        isolated += (0.002 * np.sin(2.0 * np.pi * 60.0 * time))[:, np.newaxis]
        isolated_result = _analyze(isolated)
        self.assertEqual(isolated_result.hum.status, "abstained")
        self.assertEqual(
            isolated_result.hum.reasons,
            ("isolated_tone_or_musical_bass_ambiguous",),
        )

    def test_hum_channels_must_agree_on_mains_family(self) -> None:
        document = _analyze(_hum(50, channels=(50, 60)))
        self.assertEqual(document.hum.status, "abstained")
        self.assertEqual(
            document.hum.reasons,
            ("channels_or_references_disagree",),
        )
        reference_channels = {
            (item.region_label, item.channel_index)
            for item in document.hum.evidence
            if item.region_role == "reference"
        }
        self.assertEqual(
            reference_channels,
            {("lead-in", 0), ("lead-in", 1), ("lead-out", 0), ("lead-out", 1)},
        )

    def test_diffuse_stationary_rumble_proposes_but_low_music_abstains(self) -> None:
        rumble = _analyze(_diffuse_rumble())
        self.assertEqual(rumble.rumble.status, "proposed")
        self.assertEqual(rumble.rumble.observed_lower_hz, 5.0)
        self.assertEqual(rumble.rumble.observed_upper_hz, 30.0)
        self.assertGreater(rumble.rumble.confidence, 0.0)

        samples, time = _base(303)
        envelope = 0.65 + 0.35 * np.sin(2.0 * np.pi * 0.25 * time)
        bass = 0.004 * envelope * np.sin(2.0 * np.pi * 18.0 * time)
        samples += bass[:, np.newaxis]
        music = _analyze(samples)
        self.assertEqual(music.rumble.status, "abstained")
        self.assertEqual(
            music.rumble.reasons,
            ("plausible_low_frequency_music_or_tone",),
        )

    def test_rumble_channels_must_agree(self) -> None:
        document = _analyze(_diffuse_rumble(second_channel=False))
        self.assertEqual(document.rumble.status, "abstained")
        self.assertEqual(
            document.rumble.reasons,
            ("channels_or_references_disagree",),
        )

    def test_silence_and_clipping_force_explicit_abstention(self) -> None:
        silence = _analyze(np.zeros((SAMPLE_COUNT, 2), dtype=np.float64))
        self.assertEqual(
            silence.hum.reasons,
            ("silence_or_signal_below_analysis_floor",),
        )
        self.assertEqual(silence.hum.confidence, 0.0)
        self.assertEqual(
            silence.rumble.reasons,
            ("silence_or_signal_below_analysis_floor",),
        )

        clipped = _hum(60)
        clipped[10, :] = 1.0
        clipped_result = _analyze(clipped)
        self.assertEqual(
            clipped_result.hum.reasons,
            ("clipping_invalidates_continuous_noise_evidence",),
        )
        self.assertEqual(
            clipped_result.rumble.reasons,
            ("clipping_invalidates_continuous_noise_evidence",),
        )

    def test_short_valid_geometry_abstains_instead_of_guessing(self) -> None:
        sample_count = 8 * SAMPLE_RATE
        samples = np.zeros((sample_count, 1), dtype=np.float64)
        time = np.arange(sample_count, dtype=np.float64) / SAMPLE_RATE
        samples[:, 0] = 0.002 * np.sin(2.0 * np.pi * 60.0 * time)
        scope = NoiseAnalysisScope("short", 0, sample_count)
        references = (
            NoiseReferenceRegion("head", "lead_in", 0, 2 * SAMPLE_RATE),
            NoiseReferenceRegion(
                "tail",
                "lead_out",
                6 * SAMPLE_RATE,
                sample_count,
            ),
        )
        result = analyze_continuous_noise(
            samples,
            sample_rate=SAMPLE_RATE,
            scope=scope,
            noise_references=references,
        )
        self.assertEqual(result.hum.reasons, ("insufficient_reference_windows",))
        self.assertEqual(result.rumble.reasons, ("insufficient_reference_windows",))

    def test_input_contract_rejects_nonfinite_and_malformed_values(self) -> None:
        scope, references = _geometry()
        invalid: list[np.ndarray] = [
            np.zeros((SAMPLE_COUNT, 2), dtype=np.int16),
            np.zeros((SAMPLE_COUNT, 2, 1), dtype=np.float64),
            np.full((SAMPLE_COUNT, 2), 1.01, dtype=np.float64),
        ]
        nonfinite = np.zeros((SAMPLE_COUNT, 2), dtype=np.float64)
        nonfinite[0, 0] = np.nan
        invalid.append(nonfinite)
        for samples in invalid:
            with self.subTest(dtype=samples.dtype, shape=samples.shape):
                with self.assertRaises(ProjectValidationError):
                    analyze_continuous_noise(
                        samples,
                        sample_rate=SAMPLE_RATE,
                        scope=scope,
                        noise_references=references,
                    )

        malformed_references = [object(), None]
        with self.assertRaises(ProjectValidationError):
            analyze_continuous_noise(
                np.zeros((SAMPLE_COUNT, 2), dtype=np.float64),
                sample_rate=SAMPLE_RATE,
                scope=scope,
                noise_references=malformed_references,  # type: ignore[arg-type]
            )

    def test_analysis_is_deterministic_and_does_not_mutate_pcm(self) -> None:
        samples = _hum(50)
        before = samples.tobytes()
        first = _analyze(samples).to_dict()
        second = _analyze(samples).to_dict()
        self.assertEqual(first, second)
        self.assertEqual(samples.tobytes(), before)
        self.assertEqual(first["schema"], CONTINUOUS_NOISE_DOCUMENT_SCHEMA)
        body = copy.deepcopy(first)
        body_sha256 = body.pop("proposal_body_sha256")
        self.assertEqual(body_sha256, canonical_json_sha256(body))
        self.assertEqual(first["hum"]["schema"], HUM_PROPOSAL_SCHEMA)
        self.assertEqual(first["rumble"]["schema"], RUMBLE_PROPOSAL_SCHEMA)
        self.assertEqual(first["algorithm"]["id"], CONTINUOUS_NOISE_ALGORITHM_ID)
        self.assertEqual(first["policy"]["mode"], "proposal_only")
        self.assertTrue(first["policy"]["automatic_application_forbidden"])
        self.assertFalse(first["policy"]["rendering_included"])
        self.assertNotIn("safe", first["hum"])


class ContinuousNoiseSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.valid = _analyze(_hum(60)).to_dict()

    def test_strict_round_trip_and_coherent_config_hash(self) -> None:
        canonical = validate_continuous_noise_proposal_document(self.valid)
        self.assertEqual(canonical, self.valid)
        self.assertEqual(
            canonical["algorithm"]["config_sha256"],
            canonical_json_sha256(canonical["config"]),
        )

    def test_policy_schema_and_config_tampering_are_rejected(self) -> None:
        mutations = []
        extra = copy.deepcopy(self.valid)
        extra["unexpected"] = True
        mutations.append(extra)
        policy = copy.deepcopy(self.valid)
        policy["policy"]["automatic_application_forbidden"] = False
        mutations.append(policy)
        config = copy.deepcopy(self.valid)
        config["config"]["hum_identity_margin_db"] = 0.5
        _rehash(config)
        mutations.append(config)
        schema = copy.deepcopy(self.valid)
        schema["hum"]["schema"] = "groove-serpent.hum-proposal/999"
        mutations.append(schema)
        for value in mutations:
            with self.subTest(value=value):
                with self.assertRaises(ProjectValidationError):
                    validate_continuous_noise_proposal_document(value)

    def test_semantically_forged_evidence_and_confidence_are_rejected(self) -> None:
        missing = copy.deepcopy(self.valid)
        missing["hum"]["evidence"].pop()
        _rehash(missing)

        duplicate = copy.deepcopy(self.valid)
        duplicate["rumble"]["evidence"][1] = copy.deepcopy(duplicate["rumble"]["evidence"][0])
        _rehash(duplicate)

        wrong_role = copy.deepcopy(self.valid)
        wrong_role["hum"]["evidence"][0]["region_role"] = "program"
        _rehash(wrong_role)

        unsupported_target = copy.deepcopy(self.valid)
        unsupported_target["hum"]["detected_harmonics"] = [1]
        _rehash(unsupported_target)

        abstained = _analyze(np.zeros((SAMPLE_COUNT, 2), dtype=np.float64)).to_dict()
        abstained["rumble"]["confidence"] = 0.4
        _rehash(abstained)

        for value in (missing, duplicate, wrong_role, unsupported_target, abstained):
            with self.subTest(value=value):
                with self.assertRaises(ProjectValidationError):
                    validate_continuous_noise_proposal_document(value)


if __name__ == "__main__":
    unittest.main()
