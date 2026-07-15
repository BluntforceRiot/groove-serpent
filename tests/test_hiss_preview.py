from __future__ import annotations

import copy
import hashlib
import unittest

import numpy as np

from groove_serpent.continuous_noise import NoiseAnalysisScope, NoiseReferenceRegion
from groove_serpent.errors import ProjectValidationError
from groove_serpent.hiss_preview import (
    HISS_PREVIEW_RECEIPT_SCHEMA,
    HISS_PREVIEW_RECIPE_SCHEMA,
    HISS_PREVIEW_RENDER_SCHEMA,
    HISS_PROPOSAL_SCHEMA,
    HISS_REVIEW_ATTESTATION_SCHEMA,
    REVIEW_ACKNOWLEDGEMENT,
    REVIEW_DECISION,
    HissProposal,
    HissPreviewConfig,
    HissPreviewRecipe,
    analyze_hiss,
    create_hiss_preview_recipe,
    render_hiss_preview,
    validate_hiss_preview_receipt,
    validate_hiss_preview_render_manifest,
    validate_hiss_proposal,
)
from groove_serpent.publication import canonical_json_sha256


SAMPLE_RATE = 24_000
SECONDS = 10
SAMPLE_COUNT = SAMPLE_RATE * SECONDS


def _geometry(
    *,
    offset: int = 0,
) -> tuple[NoiseAnalysisScope, tuple[NoiseReferenceRegion, ...]]:
    scope = NoiseAnalysisScope(
        "side-a",
        offset,
        offset + SAMPLE_COUNT,
    )
    references = (
        NoiseReferenceRegion(
            "lead-in",
            "lead_in",
            offset,
            offset + 2 * SAMPLE_RATE,
        ),
        NoiseReferenceRegion(
            "lead-out",
            "lead_out",
            offset + 8 * SAMPLE_RATE,
            offset + SAMPLE_COUNT,
        ),
    )
    return scope, references


def _band_noise(seed: int, count: int, amplitude: float) -> np.ndarray:
    source = np.random.default_rng(seed).normal(0.0, 1.0, count)
    spectrum = np.fft.rfft(source)
    frequencies = np.fft.rfftfreq(count, d=1.0 / SAMPLE_RATE)
    spectrum[(frequencies < 6_000.0) | (frequencies > 10_800.0)] = 0.0
    result = np.fft.irfft(spectrum, n=count)
    result *= amplitude / float(np.std(result))
    return result


def _capture(
    *,
    channel_two_scale: float = 0.95,
    offset: int = 0,
) -> tuple[np.ndarray, NoiseAnalysisScope, tuple[NoiseReferenceRegion, ...]]:
    total = SAMPLE_COUNT + 2 * offset
    samples = np.zeros((total, 2), dtype=np.float64)
    samples[:, 0] = _band_noise(1001, total, 0.001)
    samples[:, 1] = _band_noise(1002, total, 0.001 * channel_two_scale)
    time = np.arange(total, dtype=np.float64) / SAMPLE_RATE
    program = slice(offset + 2 * SAMPLE_RATE, offset + 8 * SAMPLE_RATE)
    music = 0.03 * np.sin(2.0 * np.pi * 440.0 * time[program])
    samples[program, :] += music[:, np.newaxis]
    scope, references = _geometry(offset=offset)
    return samples, scope, references


def _proposal(
    samples: np.ndarray,
    scope: NoiseAnalysisScope,
    references: tuple[NoiseReferenceRegion, ...],
) -> HissProposal:
    return analyze_hiss(
        samples,
        sample_rate=SAMPLE_RATE,
        scope=scope,
        noise_references=references,
    )


def _attestation(
    proposal: HissProposal,
    scope: NoiseAnalysisScope,
    *,
    seed: bytes = b"explicit-hiss-owner-audition-request",
) -> dict[str, object]:
    return {
        "schema": HISS_REVIEW_ATTESTATION_SCHEMA,
        "attestation_token": hashlib.sha256(seed).hexdigest(),
        "decision": REVIEW_DECISION,
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "selected_scope": scope.to_dict(),
        "acknowledgement": REVIEW_ACKNOWLEDGEMENT,
    }


def _fixture() -> tuple[np.ndarray, HissProposal, HissPreviewRecipe]:
    samples, scope, references = _capture()
    proposal = _proposal(samples, scope, references)
    recipe = create_hiss_preview_recipe(
        proposal,
        _attestation(proposal, scope),
    )
    return samples, proposal, recipe


def _rehash(value: dict[str, object], field: str) -> None:
    body = copy.deepcopy(value)
    del body[field]
    value[field] = canonical_json_sha256(body)


class HissEvidenceTests(unittest.TestCase):
    def test_stationary_broadband_references_propose_with_review_only_authority(self) -> None:
        samples, scope, references = _capture()

        proposal = _proposal(samples, scope, references)

        self.assertEqual(proposal.schema, HISS_PROPOSAL_SCHEMA)
        self.assertEqual(proposal.status, "proposed")
        self.assertGreater(proposal.confidence, 0.0)
        self.assertEqual(
            proposal.reasons,
            (
                "stationary_broadband_high_frequency_noise_agrees_across_reviewed_"
                "references_and_channels",
            ),
        )
        self.assertEqual(len(proposal.evidence), 4)
        self.assertTrue(all(item.window_count == 2 for item in proposal.evidence))
        self.assertTrue(all(item.qualifying_persistence == 1.0 for item in proposal.evidence))
        self.assertTrue(proposal.policy["requires_owner_audition"])
        self.assertFalse(proposal.policy["quality_neutrality_claimed"])
        self.assertFalse(proposal.policy["source_audio_modified"])
        self.assertEqual(validate_hiss_proposal(proposal.to_dict()), proposal.to_dict())

    def test_music_like_bright_tones_and_program_only_brightness_abstain(self) -> None:
        time = np.arange(SAMPLE_COUNT, dtype=np.float64) / SAMPLE_RATE
        tones = np.zeros((SAMPLE_COUNT, 2), dtype=np.float64)
        for frequency, amplitude in ((6_700.0, 0.0007), (8_100.0, 0.0006), (9_700.0, 0.0005)):
            tones += (amplitude * np.sin(2.0 * np.pi * frequency * time))[:, np.newaxis]
        scope, references = _geometry()

        tonal = _proposal(tones, scope, references)

        self.assertEqual(tonal.status, "abstained")
        self.assertEqual(tonal.reasons, ("music_like_tonality_or_bright_content",))

        program_only = np.random.default_rng(123).normal(
            0.0,
            0.000001,
            (SAMPLE_COUNT, 2),
        )
        bright = _band_noise(124, 6 * SAMPLE_RATE, 0.02)
        program_only[2 * SAMPLE_RATE : 8 * SAMPLE_RATE, :] += bright[:, np.newaxis]
        result = _proposal(program_only, scope, references)
        self.assertEqual(result.status, "abstained")
        self.assertEqual(result.reasons, ("silence_or_signal_below_analysis_floor",))

    def test_transient_references_and_channel_disagreement_abstain(self) -> None:
        samples, scope, references = _capture()
        envelope = np.full(SAMPLE_COUNT, 0.01, dtype=np.float64)
        for start in (0, SAMPLE_RATE, 8 * SAMPLE_RATE, 9 * SAMPLE_RATE):
            envelope[start : start + SAMPLE_RATE // 8] = 4.0
        transient = samples.copy()
        transient[:, 0] = _band_noise(201, SAMPLE_COUNT, 0.001) * envelope
        transient[:, 1] = _band_noise(202, SAMPLE_COUNT, 0.001) * envelope
        time = np.arange(SAMPLE_COUNT, dtype=np.float64) / SAMPLE_RATE
        program = slice(2 * SAMPLE_RATE, 8 * SAMPLE_RATE)
        transient[program, :] += (0.03 * np.sin(2.0 * np.pi * 440.0 * time[program]))[:, np.newaxis]

        transient_result = _proposal(transient, scope, references)
        self.assertEqual(transient_result.status, "abstained")
        self.assertEqual(
            transient_result.reasons,
            ("transient_or_temporally_unstable_reference",),
        )

        disagreement, scope, references = _capture(channel_two_scale=0.15)
        disagreement_result = _proposal(disagreement, scope, references)
        self.assertEqual(disagreement_result.status, "abstained")
        self.assertEqual(
            disagreement_result.reasons,
            ("channels_or_references_disagree",),
        )

    def test_insufficient_references_or_windows_abstain(self) -> None:
        samples, scope, references = _capture()
        one_reference = _proposal(samples, scope, references[:1])
        self.assertEqual(one_reference.status, "abstained")
        self.assertEqual(one_reference.reasons, ("insufficient_reference_regions",))

        short_references = (
            NoiseReferenceRegion("short-in", "lead_in", 0, SAMPLE_RATE // 2),
            NoiseReferenceRegion(
                "short-out",
                "lead_out",
                SAMPLE_COUNT - SAMPLE_RATE // 2,
                SAMPLE_COUNT,
            ),
        )
        short = _proposal(samples, scope, short_references)
        self.assertEqual(short.status, "abstained")
        self.assertEqual(short.reasons, ("insufficient_reference_windows",))

    def test_nonfinite_is_rejected_and_clipping_abstains(self) -> None:
        samples, scope, references = _capture()
        nonfinite = samples.copy()
        nonfinite[0, 0] = np.nan
        with self.assertRaises(ProjectValidationError):
            _proposal(nonfinite, scope, references)

        clipped = samples.copy()
        clipped[3 * SAMPLE_RATE, 0] = 1.0
        result = _proposal(clipped, scope, references)
        self.assertEqual(result.status, "abstained")
        self.assertEqual(result.reasons, ("clipping_invalidates_hiss_evidence",))
        with self.assertRaises(ProjectValidationError):
            create_hiss_preview_recipe(result, _attestation(result, scope))


class HissPreviewTests(unittest.TestCase):
    def test_preview_is_deterministic_scoped_bounded_and_immutable(self) -> None:
        samples, proposal, recipe = _fixture()
        before = samples.tobytes()

        first = render_hiss_preview(samples, proposal, recipe)
        second = render_hiss_preview(samples, proposal, recipe)

        self.assertEqual(samples.tobytes(), before)
        self.assertTrue(np.array_equal(first.original, second.original))
        self.assertTrue(np.array_equal(first.proposed, second.proposed))
        self.assertTrue(np.array_equal(first.removed, second.removed))
        self.assertEqual(first.receipt, second.receipt)
        self.assertEqual(first.render_manifest, second.render_manifest)
        self.assertEqual(recipe.schema, HISS_PREVIEW_RECIPE_SCHEMA)
        self.assertEqual(first.render_manifest["schema"], HISS_PREVIEW_RENDER_SCHEMA)
        self.assertEqual(first.receipt["schema"], HISS_PREVIEW_RECEIPT_SCHEMA)
        self.assertGreater(np.count_nonzero(first.removed), 0)
        self.assertTrue(np.array_equal(first.removed, first.original - first.proposed))
        self.assertTrue(all(first.receipt["proof"].values()))
        self.assertTrue(first.receipt["policy"]["zero_sonic_impact_not_claimed"])
        self.assertFalse(first.receipt["policy"]["quality_neutrality_claimed"])
        self.assertEqual(
            first.receipt["noise_estimate"]["method"],
            "median_reference_only_power_spectrum/1",
        )
        for metric in first.receipt["channel_metrics"]:
            self.assertLess(metric["removed_peak"], 0.02)
            self.assertLess(metric["removed_energy_ratio"], 0.01)
            self.assertLessEqual(metric["scope_high_band_reduction_db"], 1.5)
            self.assertGreater(metric["reference_high_band_reduction_db"], 0.01)
            self.assertGreater(metric["removed_high_band_fraction"], 0.80)
        audition = first.receipt["audition"]
        self.assertEqual(audition["original_linear_gain"], 1.0)
        self.assertLessEqual(abs(audition["proposed_gain_db"]), 0.25)
        self.assertEqual(audition["residue_monitor_linear_gain"], 4.0)
        self.assertTrue(first.receipt["policy"]["attestation_is_not_human_audition_proof"])
        self.assertFalse(first.original.flags.writeable)
        self.assertFalse(first.proposed.flags.writeable)
        self.assertFalse(first.removed.flags.writeable)
        with self.assertRaises(ValueError):
            first.proposed[0, 0] = 0.0

    def test_outside_scope_and_scope_edges_are_exactly_preserved(self) -> None:
        samples, scope, references = _capture(offset=SAMPLE_RATE)
        proposal = _proposal(samples, scope, references)
        recipe = create_hiss_preview_recipe(
            proposal,
            _attestation(proposal, scope, seed=b"interior-hiss-scope"),
        )

        result = render_hiss_preview(samples, proposal, recipe)

        self.assertTrue(
            np.array_equal(
                result.proposed[: scope.start_sample],
                result.original[: scope.start_sample],
            )
        )
        self.assertTrue(
            np.array_equal(
                result.proposed[scope.end_sample_exclusive :],
                result.original[scope.end_sample_exclusive :],
            )
        )
        self.assertFalse(np.any(result.removed[: scope.start_sample]))
        self.assertFalse(np.any(result.removed[scope.end_sample_exclusive :]))
        self.assertFalse(np.any(result.removed[scope.start_sample]))
        self.assertFalse(np.any(result.removed[scope.end_sample_exclusive - 1]))

    def test_attestation_is_exact_and_does_not_imply_approval(self) -> None:
        samples, scope, references = _capture()
        proposal = _proposal(samples, scope, references)
        bad = _attestation(proposal, scope)
        bad["acknowledgement"] = "I listened and approve"

        with self.assertRaises(ProjectValidationError):
            create_hiss_preview_recipe(proposal, bad)

        recipe = create_hiss_preview_recipe(proposal, _attestation(proposal, scope))
        self.assertTrue(recipe.policy["attestation_is_not_human_audition_proof"])
        self.assertTrue(recipe.policy["automatic_application_forbidden"])
        self.assertTrue(recipe.policy["automatic_publication_forbidden"])

    def test_reference_estimate_excludes_program_and_hard_caps_fail_closed(self) -> None:
        samples, scope, references = _capture()
        proposal = _proposal(samples, scope, references)
        recipe = create_hiss_preview_recipe(proposal, _attestation(proposal, scope))
        first = render_hiss_preview(samples, proposal, recipe)

        changed_program = samples.copy()
        time = np.arange(SAMPLE_COUNT, dtype=np.float64) / SAMPLE_RATE
        program = slice(2 * SAMPLE_RATE, 8 * SAMPLE_RATE)
        changed_program[program, :] += (0.01 * np.sin(2.0 * np.pi * 880.0 * time[program]))[
            :, np.newaxis
        ]
        changed_proposal = _proposal(changed_program, scope, references)
        changed_recipe = create_hiss_preview_recipe(
            changed_proposal,
            _attestation(changed_proposal, scope, seed=b"changed-program"),
        )
        second = render_hiss_preview(
            changed_program,
            changed_proposal,
            changed_recipe,
        )
        self.assertEqual(
            first.receipt["noise_estimate"]["noise_psd_sha256"],
            second.receipt["noise_estimate"]["noise_psd_sha256"],
        )

        strict_recipe = create_hiss_preview_recipe(
            proposal,
            _attestation(proposal, scope, seed=b"strict-hiss-caps"),
            config=HissPreviewConfig(
                maximum_removed_peak=0.000001,
                maximum_removed_energy_ratio=0.000001,
            ),
        )
        with self.assertRaises(ProjectValidationError):
            render_hiss_preview(samples, proposal, strict_recipe)

    def test_tampered_and_rehashed_evidence_or_stale_runtime_fail_closed(self) -> None:
        samples, proposal, recipe = _fixture()
        wrong_pcm = samples.copy()
        wrong_pcm[4 * SAMPLE_RATE, 0] += 0.000001
        with self.assertRaises(ProjectValidationError):
            render_hiss_preview(wrong_pcm, proposal, recipe)

        tampered = proposal.to_dict()
        tampered["confidence"] = 0.01
        with self.assertRaises(ProjectValidationError):
            HissProposal.from_dict(tampered)

        forged = proposal.to_dict()
        forged_evidence = forged["evidence"]
        assert isinstance(forged_evidence, list)
        assert isinstance(forged_evidence[0], dict)
        forged_evidence[0]["median_spectral_flatness"] = 0.99
        _rehash(forged, "proposal_body_sha256")
        forged_proposal = HissProposal.from_dict(forged)
        forged_recipe = create_hiss_preview_recipe(
            forged_proposal,
            _attestation(forged_proposal, forged_proposal.scope, seed=b"forged-evidence"),
        )
        with self.assertRaises(ProjectValidationError):
            render_hiss_preview(samples, forged_proposal, forged_recipe)

        stale = recipe.to_dict()
        stale_algorithm = stale["algorithm"]
        assert isinstance(stale_algorithm, dict)
        stale_algorithm["module_sha256"] = "0" * 64
        _rehash(stale, "recipe_body_sha256")
        stale_recipe = HissPreviewRecipe.from_dict(stale)
        with self.assertRaises(ProjectValidationError):
            render_hiss_preview(samples, proposal, stale_recipe)

    def test_rehashed_false_receipt_fails_independent_array_validation(self) -> None:
        samples, proposal, recipe = _fixture()
        result = render_hiss_preview(samples, proposal, recipe)
        false_receipt = copy.deepcopy(result.receipt)
        metrics = false_receipt["channel_metrics"]
        aggregate = false_receipt["aggregate"]
        assert isinstance(metrics, list)
        assert isinstance(aggregate, dict)
        for metric in metrics:
            assert isinstance(metric, dict)
            metric["removed_energy_ratio"] = 0.0
        aggregate["maximum_removed_energy_ratio"] = 0.0
        _rehash(false_receipt, "receipt_body_sha256")

        with self.assertRaises(ProjectValidationError):
            validate_hiss_preview_receipt(
                false_receipt,
                recipe=recipe,
                render_manifest=result.render_manifest,
                arrays=(result.original, result.proposed, result.removed),
            )

        self.assertEqual(
            validate_hiss_preview_render_manifest(
                result.render_manifest,
                recipe=recipe,
            ),
            result.render_manifest,
        )
        self.assertEqual(
            validate_hiss_preview_receipt(
                result.receipt,
                recipe=recipe,
                render_manifest=result.render_manifest,
                arrays=(result.original, result.proposed, result.removed),
            ),
            result.receipt,
        )


if __name__ == "__main__":
    unittest.main()
