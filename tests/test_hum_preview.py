from __future__ import annotations

import copy
import hashlib
import unittest

import numpy as np

from groove_serpent.continuous_noise import (
    ContinuousNoiseProposalDocument,
    NoiseAnalysisScope,
    NoiseReferenceRegion,
    analyze_continuous_noise,
)
from groove_serpent.errors import ProjectValidationError
from groove_serpent.hum_preview import (
    HUM_PREVIEW_RECEIPT_SCHEMA,
    HUM_PREVIEW_RENDER_SCHEMA,
    HUM_PREVIEW_RECIPE_SCHEMA,
    HUM_REVIEW_ATTESTATION_SCHEMA,
    REVIEW_ACKNOWLEDGEMENT,
    REVIEW_DECISION,
    HumPreviewRecipe,
    create_hum_preview_recipe,
    render_hum_preview,
    validate_hum_preview_render_manifest,
    validate_hum_preview_receipt,
)
from groove_serpent.publication import canonical_json_sha256


SAMPLE_RATE = 8_000
SAMPLE_COUNT = 28 * SAMPLE_RATE


def _capture(
    fundamental: int = 60,
    *,
    channel_frequencies: tuple[int, int] | None = None,
    amplitude: float = 0.002,
) -> tuple[np.ndarray, NoiseAnalysisScope, tuple[NoiseReferenceRegion, ...]]:
    time = np.arange(SAMPLE_COUNT, dtype=np.float64) / SAMPLE_RATE
    samples = np.random.default_rng(700 + fundamental).normal(
        0.0,
        0.00001,
        (SAMPLE_COUNT, 2),
    )
    frequencies = channel_frequencies or (fundamental, fundamental)
    for channel, frequency in enumerate(frequencies):
        samples[:, channel] += (
            amplitude * np.sin(2.0 * np.pi * frequency * time)
            + amplitude * 0.4 * np.sin(2.0 * np.pi * frequency * 2 * time)
            + amplitude * 0.2 * np.sin(2.0 * np.pi * frequency * 3 * time)
        )
    program = slice(6 * SAMPLE_RATE, 22 * SAMPLE_RATE)
    samples[program, :] += (0.04 * np.sin(2.0 * np.pi * 440.0 * time[program]))[:, np.newaxis]
    scope = NoiseAnalysisScope("side-a", 2 * SAMPLE_RATE, 26 * SAMPLE_RATE)
    references = (
        NoiseReferenceRegion(
            "lead-in",
            "lead_in",
            2 * SAMPLE_RATE,
            6 * SAMPLE_RATE,
        ),
        NoiseReferenceRegion(
            "lead-out",
            "lead_out",
            22 * SAMPLE_RATE,
            26 * SAMPLE_RATE,
        ),
    )
    return samples, scope, references


def _proposal(
    samples: np.ndarray,
    scope: NoiseAnalysisScope,
    references: tuple[NoiseReferenceRegion, ...],
) -> ContinuousNoiseProposalDocument:
    return analyze_continuous_noise(
        samples,
        sample_rate=SAMPLE_RATE,
        scope=scope,
        noise_references=references,
    )


def _attestation(
    proposal: ContinuousNoiseProposalDocument,
    scope: NoiseAnalysisScope,
    token_seed: bytes = b"owner-review-session",
) -> dict[str, object]:
    return {
        "schema": HUM_REVIEW_ATTESTATION_SCHEMA,
        "attestation_token": hashlib.sha256(token_seed).hexdigest(),
        "decision": REVIEW_DECISION,
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "selected_scope": scope.to_dict(),
        "acknowledgement": REVIEW_ACKNOWLEDGEMENT,
    }


def _recipe_fixture() -> tuple[
    np.ndarray,
    ContinuousNoiseProposalDocument,
    HumPreviewRecipe,
]:
    samples, scope, references = _capture()
    proposal = _proposal(samples, scope, references)
    recipe = create_hum_preview_recipe(
        proposal,
        _attestation(proposal, scope),
    )
    return samples, proposal, recipe


def _rehash(value: dict[str, object], hash_field: str) -> None:
    body = copy.deepcopy(value)
    del body[hash_field]
    value[hash_field] = canonical_json_sha256(body)


def _forge_proposal_for_pcm(
    proposal: ContinuousNoiseProposalDocument,
    samples: np.ndarray,
) -> ContinuousNoiseProposalDocument:
    value = proposal.to_dict()
    normalized = np.ascontiguousarray(samples, dtype="<f8")
    value["normalized_pcm_sha256"] = hashlib.sha256(normalized.tobytes(order="C")).hexdigest()
    _rehash(value, "proposal_body_sha256")
    return ContinuousNoiseProposalDocument.from_dict(value)


class HumPreviewPositiveTests(unittest.TestCase):
    def test_stationary_hum_preview_is_bounded_deterministic_and_immutable(self) -> None:
        samples, proposal, recipe = _recipe_fixture()
        before = samples.tobytes()

        first = render_hum_preview(samples, proposal, recipe)
        second = render_hum_preview(samples, proposal, recipe)

        self.assertEqual(samples.tobytes(), before)
        self.assertTrue(np.array_equal(first.original, second.original))
        self.assertTrue(np.array_equal(first.proposed, second.proposed))
        self.assertTrue(np.array_equal(first.removed, second.removed))
        self.assertEqual(first.receipt, second.receipt)
        self.assertEqual(first.render_manifest, second.render_manifest)
        self.assertEqual(recipe.schema, HUM_PREVIEW_RECIPE_SCHEMA)
        self.assertEqual(first.receipt["schema"], HUM_PREVIEW_RECEIPT_SCHEMA)
        self.assertEqual(
            first.render_manifest["schema"],
            HUM_PREVIEW_RENDER_SCHEMA,
        )
        self.assertEqual(
            first.receipt["proposal_body_sha256"],
            proposal.proposal_body_sha256,
        )

        scope = proposal.scope
        self.assertTrue(
            np.array_equal(
                first.proposed[: scope.start_sample],
                first.original[: scope.start_sample],
            )
        )
        self.assertTrue(
            np.array_equal(
                first.proposed[scope.end_sample_exclusive :],
                first.original[scope.end_sample_exclusive :],
            )
        )
        self.assertFalse(np.any(first.removed[: scope.start_sample]))
        self.assertFalse(np.any(first.removed[scope.end_sample_exclusive :]))
        self.assertFalse(np.any(first.removed[scope.start_sample]))
        self.assertFalse(np.any(first.removed[scope.end_sample_exclusive - 1]))
        error = np.max(np.abs(first.original - (first.proposed + first.removed)))
        self.assertLessEqual(error, np.finfo(np.float64).eps * 2.0)
        self.assertGreater(np.count_nonzero(first.removed), 0)

        audition = first.receipt["audition"]
        self.assertEqual(audition["original_linear_gain"], 1.0)
        self.assertLess(abs(audition["proposed_gain_db"]), 0.15)
        self.assertEqual(audition["residue_monitor_linear_gain"], 16.0)
        self.assertTrue(audition["raw_arrays_are_gain_neutral"])
        self.assertTrue(all(first.receipt["proof"].values()))
        self.assertFalse(first.receipt["policy"]["quality_neutrality_claimed"])
        self.assertTrue(first.receipt["policy"]["attestation_is_not_human_audition_proof"])
        self.assertTrue(
            all(
                metric["removed_energy_ratio"] < 0.02 for metric in first.receipt["channel_metrics"]
            )
        )
        self.assertFalse(first.original.flags.writeable)
        self.assertFalse(first.proposed.flags.writeable)
        self.assertFalse(first.removed.flags.writeable)
        with self.assertRaises(ValueError):
            first.proposed[0, 0] = 0.0
        self.assertTrue(
            all(
                harmonic["retained_residual_ratio"] < 0.35
                for metric in first.receipt["channel_metrics"]
                for harmonic in metric["harmonics"]
            )
        )

    def test_mono_shape_and_raw_difference_are_preserved(self) -> None:
        stereo, scope, references = _capture(50)
        mono = stereo[:, 0].copy()
        proposal = _proposal(mono, scope, references)
        recipe = create_hum_preview_recipe(
            proposal,
            _attestation(proposal, scope, b"mono-review"),
        )
        result = render_hum_preview(mono, proposal, recipe)
        self.assertEqual(result.original.ndim, 1)
        self.assertEqual(result.proposed.shape, mono.shape)
        self.assertEqual(result.removed.shape, mono.shape)
        self.assertTrue(np.array_equal(result.removed, result.original - result.proposed))


class HumPreviewAuthorityAndRejectionTests(unittest.TestCase):
    def test_review_attestation_is_mandatory_exact_and_not_an_approval_claim(self) -> None:
        samples, scope, references = _capture()
        proposal = _proposal(samples, scope, references)
        valid = _attestation(proposal, scope)
        invalid: list[dict[str, object]] = []
        missing = copy.deepcopy(valid)
        del missing["attestation_token"]
        invalid.append(missing)
        zeros = copy.deepcopy(valid)
        zeros["attestation_token"] = "0" * 64
        invalid.append(zeros)
        wrong_decision = copy.deepcopy(valid)
        wrong_decision["decision"] = "approved"
        invalid.append(wrong_decision)
        wrong_acknowledgement = copy.deepcopy(valid)
        wrong_acknowledgement["acknowledgement"] = "owner listened"
        invalid.append(wrong_acknowledgement)
        stale = copy.deepcopy(valid)
        stale["proposal_body_sha256"] = "1" * 64
        invalid.append(stale)
        wrong_scope = copy.deepcopy(valid)
        wrong_scope["selected_scope"] = {
            "label": "side-a",
            "start_sample": scope.start_sample + 1,
            "end_sample_exclusive": scope.end_sample_exclusive,
        }
        invalid.append(wrong_scope)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ProjectValidationError):
                    create_hum_preview_recipe(proposal, value)

        recipe = create_hum_preview_recipe(proposal, valid)
        self.assertTrue(recipe.policy["attestation_is_not_human_audition_proof"])
        self.assertNotIn("approved", recipe.to_dict())

    def test_abstained_musical_tone_and_stereo_disagreement_cannot_render(self) -> None:
        samples, scope, references = _capture()
        time = np.arange(SAMPLE_COUNT, dtype=np.float64) / SAMPLE_RATE
        musical = np.zeros((SAMPLE_COUNT, 2), dtype=np.float64)
        musical += (
            0.12 * np.sin(2.0 * np.pi * 60.0 * time) + 0.05 * np.sin(2.0 * np.pi * 120.0 * time)
        )[:, np.newaxis]
        musical_proposal = _proposal(musical, scope, references)
        self.assertEqual(musical_proposal.hum.status, "abstained")
        with self.assertRaisesRegex(ProjectValidationError, "abstained"):
            create_hum_preview_recipe(
                musical_proposal,
                _attestation(musical_proposal, scope, b"musical"),
            )

        disagreeing, disagree_scope, disagree_refs = _capture(
            50,
            channel_frequencies=(50, 60),
        )
        disagreement = _proposal(disagreeing, disagree_scope, disagree_refs)
        self.assertEqual(disagreement.hum.status, "abstained")
        self.assertEqual(
            disagreement.hum.reasons,
            ("channels_or_references_disagree",),
        )
        with self.assertRaisesRegex(ProjectValidationError, "abstained"):
            create_hum_preview_recipe(
                disagreement,
                _attestation(disagreement, disagree_scope, b"disagreement"),
            )

    def test_changed_nonfinite_clipped_and_unstable_pcm_fail_closed(self) -> None:
        samples, proposal, recipe = _recipe_fixture()
        with self.assertRaisesRegex(ProjectValidationError, "NumPy array"):
            render_hum_preview(
                [0.0],  # type: ignore[arg-type]
                proposal,
                recipe,
            )
        changed = samples.copy()
        changed[100, 0] += 0.0001
        with self.assertRaisesRegex(ProjectValidationError, "does not match"):
            render_hum_preview(changed, proposal, recipe)

        nonfinite = samples.copy()
        nonfinite[100, 0] = np.nan
        with self.assertRaisesRegex(ProjectValidationError, "finite"):
            render_hum_preview(nonfinite, proposal, recipe)

        clipped = samples.copy()
        clipped[100, :] = 1.0
        clipped_proposal = _forge_proposal_for_pcm(proposal, clipped)
        clipped_recipe = create_hum_preview_recipe(
            clipped_proposal,
            _attestation(clipped_proposal, clipped_proposal.scope, b"clipped"),
        )
        with self.assertRaisesRegex(ProjectValidationError, "clipped"):
            render_hum_preview(clipped, clipped_proposal, clipped_recipe)

        unstable = samples.copy()
        time = np.arange(SAMPLE_COUNT, dtype=np.float64) / SAMPLE_RATE
        for index, start in enumerate(
            (
                2 * SAMPLE_RATE,
                4 * SAMPLE_RATE,
                22 * SAMPLE_RATE,
                24 * SAMPLE_RATE,
            )
        ):
            end = start + 2 * SAMPLE_RATE
            sign = 1.0 if index % 2 == 0 else -1.0
            unstable[start:end, :] += (sign * 0.003 * np.sin(2.0 * np.pi * 60.0 * time[start:end]))[
                :, np.newaxis
            ]
        unstable_proposal = _forge_proposal_for_pcm(proposal, unstable)
        unstable_recipe = create_hum_preview_recipe(
            unstable_proposal,
            _attestation(unstable_proposal, unstable_proposal.scope, b"unstable"),
        )
        with self.assertRaisesRegex(ProjectValidationError, "unstable"):
            render_hum_preview(unstable, unstable_proposal, unstable_recipe)

    def test_excessive_fitted_removal_fails_closed(self) -> None:
        samples, scope, references = _capture(amplitude=0.002)
        proposal = _proposal(samples, scope, references)
        loud, _scope, _references = _capture(amplitude=0.04)
        forged = _forge_proposal_for_pcm(proposal, loud)
        recipe = create_hum_preview_recipe(
            forged,
            _attestation(forged, scope, b"excessive"),
        )
        with self.assertRaisesRegex(ProjectValidationError, "amplitude|RMS|peak|energy"):
            render_hum_preview(loud, forged, recipe)


class HumPreviewSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.samples, self.proposal, self.recipe = _recipe_fixture()
        self.result = render_hum_preview(
            self.samples,
            self.proposal,
            self.recipe,
        )

    def test_recipe_and_receipt_round_trip_strictly(self) -> None:
        parsed = HumPreviewRecipe.from_dict(self.recipe.to_dict())
        self.assertEqual(parsed.to_dict(), self.recipe.to_dict())
        receipt = validate_hum_preview_receipt(
            self.result.receipt,
            recipe=self.recipe,
            render_manifest=self.result.render_manifest,
            arrays=(
                self.result.original,
                self.result.proposed,
                self.result.removed,
            ),
        )
        self.assertEqual(receipt, self.result.receipt)
        render = validate_hum_preview_render_manifest(
            self.result.render_manifest,
            recipe=self.recipe,
        )
        self.assertEqual(render, self.result.render_manifest)

    def test_recipe_tampering_and_stale_module_are_rejected(self) -> None:
        raw = self.recipe.to_dict()
        extra = copy.deepcopy(raw)
        extra["unexpected"] = True
        with self.assertRaises(ProjectValidationError):
            HumPreviewRecipe.from_dict(extra)

        config = copy.deepcopy(raw)
        config["config"]["edge_fade_ms"] = 30
        _rehash(config, "recipe_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "config identity"):
            HumPreviewRecipe.from_dict(config)

        stale = copy.deepcopy(raw)
        stale["algorithm"]["module_sha256"] = "1" * 64
        _rehash(stale, "recipe_body_sha256")
        parsed_stale = HumPreviewRecipe.from_dict(stale)
        with self.assertRaisesRegex(ProjectValidationError, "module identity"):
            render_hum_preview(self.samples, self.proposal, parsed_stale)

    def test_receipt_tampering_fails_even_after_rehash(self) -> None:
        raw = copy.deepcopy(self.result.receipt)
        raw["proof"]["source_array_immutable"] = False
        _rehash(raw, "receipt_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "proof"):
            validate_hum_preview_receipt(raw, recipe=self.recipe)

        missing = copy.deepcopy(self.result.receipt)
        missing["channel_metrics"].pop()
        _rehash(missing, "receipt_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "every channel"):
            validate_hum_preview_receipt(missing, recipe=self.recipe)

        different_recipe = copy.deepcopy(self.result.receipt)
        different_recipe["recipe_body_sha256"] = "2" * 64
        _rehash(different_recipe, "receipt_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "different recipe"):
            validate_hum_preview_receipt(different_recipe, recipe=self.recipe)

        mismatched_gain = copy.deepcopy(self.result.receipt)
        mismatched_gain["audition"]["proposed_linear_gain"] = 1.1
        _rehash(mismatched_gain, "receipt_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "linear value"):
            validate_hum_preview_receipt(mismatched_gain, recipe=self.recipe)

        excessive_metric = copy.deepcopy(self.result.receipt)
        excessive_metric["channel_metrics"][0]["removed_peak"] = 0.5
        excessive_metric["aggregate"]["maximum_removed_peak"] = 0.5
        _rehash(excessive_metric, "receipt_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "peak"):
            validate_hum_preview_receipt(excessive_metric, recipe=self.recipe)

        false_aggregate = copy.deepcopy(self.result.receipt)
        false_aggregate["aggregate"]["maximum_removed_peak"] = 0.0
        _rehash(false_aggregate, "receipt_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "aggregate"):
            validate_hum_preview_receipt(false_aggregate, recipe=self.recipe)

        wrong_input = copy.deepcopy(self.result.receipt)
        wrong_input["input"]["normalized_pcm_sha256"] = "3" * 64
        _rehash(wrong_input, "receipt_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "input identity"):
            validate_hum_preview_receipt(wrong_input, recipe=self.recipe)

        render_gain = copy.deepcopy(self.result.render_manifest)
        render_gain["audition"]["proposed_linear_gain"] = 1.1
        _rehash(render_gain, "render_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "linear value"):
            validate_hum_preview_render_manifest(render_gain, recipe=self.recipe)


if __name__ == "__main__":
    unittest.main()
