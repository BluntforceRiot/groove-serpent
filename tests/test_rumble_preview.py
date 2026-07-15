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
from groove_serpent.publication import canonical_json_sha256
from groove_serpent.rumble_preview import (
    REVIEW_ACKNOWLEDGEMENT,
    REVIEW_DECISION,
    RUMBLE_PREVIEW_RECEIPT_SCHEMA,
    RUMBLE_PREVIEW_RECIPE_SCHEMA,
    RUMBLE_PREVIEW_RENDER_SCHEMA,
    RUMBLE_REVIEW_ATTESTATION_SCHEMA,
    RumblePreviewConfig,
    RumblePreviewRecipe,
    create_rumble_preview_recipe,
    render_rumble_preview,
    validate_rumble_preview_receipt,
    validate_rumble_preview_render_manifest,
)


SAMPLE_RATE = 8_000


def _capture(
    *,
    seconds: int = 24,
    second_channel: bool = True,
    amplitude: float = 0.004,
) -> tuple[np.ndarray, NoiseAnalysisScope, tuple[NoiseReferenceRegion, ...]]:
    count = seconds * SAMPLE_RATE
    time = np.arange(count, dtype=np.float64) / SAMPLE_RATE
    samples = np.random.default_rng(900 + seconds).normal(
        0.0,
        0.00001,
        (count, 2),
    )
    source = np.random.default_rng(901 + seconds).normal(0.0, 1.0, count)
    frequencies = np.fft.rfftfreq(count, d=1.0 / SAMPLE_RATE)
    spectrum = np.fft.rfft(source)
    spectrum[(frequencies < 5.0) | (frequencies > 28.0)] = 0.0
    rumble = np.fft.irfft(spectrum, n=count)
    rumble *= amplitude / float(np.std(rumble))
    samples[:, 0] += rumble
    if second_channel:
        samples[:, 1] += rumble * 0.93
    else:
        samples[:, 1] += np.random.default_rng(902).normal(0.0, 0.0001, count)
    reference_seconds = 4
    program = slice(reference_seconds * SAMPLE_RATE, (seconds - reference_seconds) * SAMPLE_RATE)
    samples[program, :] += (
        0.03 * np.sin(2.0 * np.pi * 440.0 * time[program])
    )[:, np.newaxis]
    scope = NoiseAnalysisScope("side-a", 0, count)
    references = (
        NoiseReferenceRegion(
            "lead-in",
            "lead_in",
            0,
            reference_seconds * SAMPLE_RATE,
        ),
        NoiseReferenceRegion(
            "lead-out",
            "lead_out",
            (seconds - reference_seconds) * SAMPLE_RATE,
            count,
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
    seed: bytes = b"rumble-owner-review",
) -> dict[str, object]:
    return {
        "schema": RUMBLE_REVIEW_ATTESTATION_SCHEMA,
        "attestation_token": hashlib.sha256(seed).hexdigest(),
        "decision": REVIEW_DECISION,
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "selected_scope": scope.to_dict(),
        "acknowledgement": REVIEW_ACKNOWLEDGEMENT,
    }


def _fixture() -> tuple[
    np.ndarray,
    ContinuousNoiseProposalDocument,
    RumblePreviewRecipe,
]:
    samples, scope, references = _capture()
    proposal = _proposal(samples, scope, references)
    recipe = create_rumble_preview_recipe(
        proposal,
        _attestation(proposal, scope),
    )
    return samples, proposal, recipe


def _rehash(value: dict[str, object], field: str) -> None:
    body = copy.deepcopy(value)
    del body[field]
    value[field] = canonical_json_sha256(body)


def _forge_proposal_for_pcm(
    proposal: ContinuousNoiseProposalDocument,
    samples: np.ndarray,
) -> ContinuousNoiseProposalDocument:
    value = proposal.to_dict()
    normalized = np.ascontiguousarray(samples, dtype="<f8")
    value["normalized_pcm_sha256"] = hashlib.sha256(
        normalized.tobytes(order="C")
    ).hexdigest()
    _rehash(value, "proposal_body_sha256")
    return ContinuousNoiseProposalDocument.from_dict(value)


class RumblePreviewPositiveTests(unittest.TestCase):
    def test_preview_is_deterministic_bounded_scoped_and_immutable(self) -> None:
        samples, proposal, recipe = _fixture()
        before = samples.tobytes()

        first = render_rumble_preview(samples, proposal, recipe)
        second = render_rumble_preview(samples, proposal, recipe)

        self.assertEqual(samples.tobytes(), before)
        self.assertTrue(np.array_equal(first.original, second.original))
        self.assertTrue(np.array_equal(first.proposed, second.proposed))
        self.assertTrue(np.array_equal(first.removed, second.removed))
        self.assertEqual(first.receipt, second.receipt)
        self.assertEqual(first.render_manifest, second.render_manifest)
        self.assertEqual(recipe.schema, RUMBLE_PREVIEW_RECIPE_SCHEMA)
        self.assertEqual(first.receipt["schema"], RUMBLE_PREVIEW_RECEIPT_SCHEMA)
        self.assertEqual(
            first.render_manifest["schema"],
            RUMBLE_PREVIEW_RENDER_SCHEMA,
        )
        scope = proposal.scope
        self.assertFalse(np.any(first.removed[scope.end_sample_exclusive :]))
        self.assertFalse(np.any(first.removed[scope.start_sample]))
        self.assertFalse(np.any(first.removed[scope.end_sample_exclusive - 1]))
        self.assertGreater(np.count_nonzero(first.removed), 0)
        self.assertTrue(np.array_equal(first.removed, first.original - first.proposed))
        self.assertLessEqual(
            np.max(np.abs(first.original - (first.proposed + first.removed))),
            np.finfo(np.float64).eps * 2.0,
        )
        self.assertTrue(all(first.receipt["proof"].values()))
        self.assertFalse(first.receipt["policy"]["quality_neutrality_claimed"])
        self.assertTrue(
            first.receipt["policy"]["attestation_is_not_human_audition_proof"]
        )
        self.assertTrue(first.receipt["filter"]["attenuation_only"])
        self.assertLess(
            abs(first.receipt["filter"]["response_at_comparison_lower_db"]),
            0.25,
        )
        for metric in first.receipt["channel_metrics"]:
            self.assertLess(metric["removed_energy_ratio"], 0.02)
            self.assertGreater(metric["reference_low_band_reduction_db"], 0.20)
            self.assertGreater(
                metric["removed_energy_below_comparison_fraction"],
                0.85,
            )
        audition = first.receipt["audition"]
        self.assertEqual(audition["original_linear_gain"], 1.0)
        self.assertLess(abs(audition["proposed_gain_db"]), 0.25)
        self.assertEqual(audition["residue_monitor_linear_gain"], 8.0)
        self.assertTrue(audition["raw_arrays_are_gain_neutral"])
        self.assertFalse(first.original.flags.writeable)
        self.assertFalse(first.proposed.flags.writeable)
        self.assertFalse(first.removed.flags.writeable)
        with self.assertRaises(ValueError):
            first.proposed[0, 0] = 0.0

    def test_nonselected_capture_frames_remain_bit_identical(self) -> None:
        interior, _scope, _references = _capture()
        padding = np.random.default_rng(777).normal(
            0.0,
            0.00001,
            (2 * SAMPLE_RATE, 2),
        )
        samples = np.concatenate((padding, interior, padding.copy()), axis=0)
        scope = NoiseAnalysisScope(
            "side-a",
            2 * SAMPLE_RATE,
            26 * SAMPLE_RATE,
        )
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
        proposal = _proposal(samples, scope, references)
        recipe = create_rumble_preview_recipe(
            proposal,
            _attestation(proposal, scope, b"interior-scope"),
        )
        result = render_rumble_preview(samples, proposal, recipe)
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

    def test_mono_shape_and_raw_difference_are_preserved(self) -> None:
        stereo, scope, references = _capture()
        mono = stereo[:, 0].copy()
        proposal = _proposal(mono, scope, references)
        recipe = create_rumble_preview_recipe(
            proposal,
            _attestation(proposal, scope, b"mono-rumble-review"),
        )
        result = render_rumble_preview(mono, proposal, recipe)
        self.assertEqual(result.original.ndim, 1)
        self.assertEqual(result.proposed.shape, mono.shape)
        self.assertEqual(result.removed.shape, mono.shape)
        self.assertTrue(np.array_equal(result.removed, result.original - result.proposed))


class RumblePreviewAuthorityAndRejectionTests(unittest.TestCase):
    def test_attestation_is_exact_mandatory_and_never_an_approval_claim(self) -> None:
        samples, scope, references = _capture()
        proposal = _proposal(samples, scope, references)
        valid = _attestation(proposal, scope)
        invalid: list[dict[str, object]] = []
        missing = copy.deepcopy(valid)
        del missing["attestation_token"]
        invalid.append(missing)
        repeated = copy.deepcopy(valid)
        repeated["attestation_token"] = "0" * 64
        invalid.append(repeated)
        decision = copy.deepcopy(valid)
        decision["decision"] = "approved"
        invalid.append(decision)
        acknowledgement = copy.deepcopy(valid)
        acknowledgement["acknowledgement"] = "owner listened"
        invalid.append(acknowledgement)
        stale = copy.deepcopy(valid)
        stale["proposal_body_sha256"] = "1" * 64
        invalid.append(stale)
        wrong_scope = copy.deepcopy(valid)
        wrong_scope["selected_scope"] = {
            "label": scope.label,
            "start_sample": scope.start_sample + 1,
            "end_sample_exclusive": scope.end_sample_exclusive,
        }
        invalid.append(wrong_scope)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ProjectValidationError):
                    create_rumble_preview_recipe(proposal, value)
        recipe = create_rumble_preview_recipe(proposal, valid)
        self.assertTrue(recipe.policy["attestation_is_not_human_audition_proof"])
        self.assertNotIn("approved", recipe.to_dict())

    def test_abstained_musical_ambiguous_and_channel_disagreement_fail_closed(self) -> None:
        count = 24 * SAMPLE_RATE
        time = np.arange(count, dtype=np.float64) / SAMPLE_RATE
        musical = np.zeros((count, 2), dtype=np.float64)
        envelope = 0.65 + 0.35 * np.sin(2.0 * np.pi * 0.25 * time)
        musical += (0.004 * envelope * np.sin(2.0 * np.pi * 18.0 * time))[
            :, np.newaxis
        ]
        _, scope, references = _capture()
        ambiguous = _proposal(musical, scope, references)
        self.assertEqual(ambiguous.rumble.status, "abstained")
        with self.assertRaisesRegex(ProjectValidationError, "abstained"):
            create_rumble_preview_recipe(
                ambiguous,
                _attestation(ambiguous, scope, b"ambiguous"),
            )

        disagreeing, disagree_scope, disagree_references = _capture(
            second_channel=False
        )
        disagreement = _proposal(
            disagreeing,
            disagree_scope,
            disagree_references,
        )
        self.assertEqual(disagreement.rumble.status, "abstained")
        self.assertEqual(
            disagreement.rumble.reasons,
            ("channels_or_references_disagree",),
        )
        with self.assertRaisesRegex(ProjectValidationError, "abstained"):
            create_rumble_preview_recipe(
                disagreement,
                _attestation(disagreement, disagree_scope, b"disagreement"),
            )

    def test_changed_nonfinite_clipped_and_tampered_evidence_fail_closed(self) -> None:
        samples, proposal, recipe = _fixture()
        changed = samples.copy()
        changed[100, 0] += 0.0001
        with self.assertRaisesRegex(ProjectValidationError, "does not match"):
            render_rumble_preview(changed, proposal, recipe)
        nonfinite = samples.copy()
        nonfinite[100, 0] = np.nan
        with self.assertRaisesRegex(ProjectValidationError, "finite"):
            render_rumble_preview(nonfinite, proposal, recipe)

        clipped = samples.copy()
        clipped[100, :] = 0.999
        clipped_proposal = _forge_proposal_for_pcm(proposal, clipped)
        clipped_recipe = create_rumble_preview_recipe(
            clipped_proposal,
            _attestation(clipped_proposal, proposal.scope, b"clipped"),
        )
        with self.assertRaisesRegex(ProjectValidationError, "clipped"):
            render_rumble_preview(clipped, clipped_proposal, clipped_recipe)

        forged = proposal.to_dict()
        forged["rumble"]["evidence"][0]["qualifying_persistence"] = 0.0
        _rehash(forged, "proposal_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "not supported"):
            create_rumble_preview_recipe(
                forged,
                _attestation(proposal, proposal.scope, b"forged"),
            )

        stale = proposal.to_dict()
        stale["algorithm"]["module_sha256"] = "1" * 64
        _rehash(stale, "proposal_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "analysis module is stale"):
            create_rumble_preview_recipe(
                stale,
                _attestation(proposal, proposal.scope, b"stale-analysis"),
            )

    def test_conservative_caps_and_short_edge_geometry_fail_closed(self) -> None:
        samples, proposal, _recipe = _fixture()
        tight = RumblePreviewConfig(maximum_removed_peak=0.000001)
        recipe = create_rumble_preview_recipe(
            proposal,
            _attestation(proposal, proposal.scope, b"tight-cap"),
            config=tight,
        )
        with self.assertRaisesRegex(ProjectValidationError, "peak"):
            render_rumble_preview(samples, proposal, recipe)

        short_samples, short_scope, short_references = _capture(seconds=12)
        short_proposal = _proposal(short_samples, short_scope, short_references)
        self.assertEqual(short_proposal.rumble.status, "proposed")
        with self.assertRaisesRegex(ProjectValidationError, "too short"):
            create_rumble_preview_recipe(
                short_proposal,
                _attestation(short_proposal, short_scope, b"short-scope"),
                config=RumblePreviewConfig(
                    reflection_padding_ms=5_000,
                    edge_fade_ms=1_000,
                ),
            )


class RumblePreviewSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.samples, self.proposal, self.recipe = _fixture()
        self.result = render_rumble_preview(
            self.samples,
            self.proposal,
            self.recipe,
        )

    def test_recipe_render_and_receipt_round_trip_strictly(self) -> None:
        parsed = RumblePreviewRecipe.from_dict(self.recipe.to_dict())
        self.assertEqual(parsed.to_dict(), self.recipe.to_dict())
        render = validate_rumble_preview_render_manifest(
            self.result.render_manifest,
            recipe=self.recipe,
        )
        self.assertEqual(render, self.result.render_manifest)
        receipt = validate_rumble_preview_receipt(
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

    def test_recipe_and_render_tampering_are_rejected_after_rehash(self) -> None:
        extra = self.recipe.to_dict()
        extra["unexpected"] = True
        with self.assertRaises(ProjectValidationError):
            RumblePreviewRecipe.from_dict(extra)

        config = self.recipe.to_dict()
        config["config"]["cutoff_hz"] = 11.0
        _rehash(config, "recipe_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "config identity"):
            RumblePreviewRecipe.from_dict(config)

        stale = self.recipe.to_dict()
        stale["algorithm"]["module_sha256"] = "1" * 64
        _rehash(stale, "recipe_body_sha256")
        parsed_stale = RumblePreviewRecipe.from_dict(stale)
        with self.assertRaisesRegex(ProjectValidationError, "module identity"):
            render_rumble_preview(self.samples, self.proposal, parsed_stale)

        render = copy.deepcopy(self.result.render_manifest)
        render["filter"]["response_at_comparison_lower_db"] = -0.2
        _rehash(render, "render_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "filter identity"):
            validate_rumble_preview_render_manifest(render, recipe=self.recipe)

    def test_receipt_tampering_and_false_metrics_are_rejected(self) -> None:
        proof = copy.deepcopy(self.result.receipt)
        proof["proof"]["source_array_immutable"] = False
        _rehash(proof, "receipt_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "proof"):
            validate_rumble_preview_receipt(proof, recipe=self.recipe)

        missing = copy.deepcopy(self.result.receipt)
        missing["channel_metrics"].pop()
        _rehash(missing, "receipt_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "every channel"):
            validate_rumble_preview_receipt(missing, recipe=self.recipe)

        false_metric = copy.deepcopy(self.result.receipt)
        changed = false_metric["channel_metrics"][0]
        changed["reference_low_band_reduction_db"] -= 0.01
        false_metric["aggregate"] = {
            **false_metric["aggregate"],
            "minimum_reference_low_band_reduction_db": min(
                item["reference_low_band_reduction_db"]
                for item in false_metric["channel_metrics"]
            ),
        }
        _rehash(false_metric, "receipt_body_sha256")
        with self.assertRaisesRegex(ProjectValidationError, "independent array analysis"):
            validate_rumble_preview_receipt(
                false_metric,
                recipe=self.recipe,
                arrays=(
                    self.result.original,
                    self.result.proposed,
                    self.result.removed,
                ),
            )

        wrong_arrays = self.result.proposed.copy()
        wrong_arrays[100, 0] += 0.000001
        with self.assertRaisesRegex(ProjectValidationError, "hashes"):
            validate_rumble_preview_receipt(
                self.result.receipt,
                recipe=self.recipe,
                arrays=(self.result.original, wrong_arrays, self.result.removed),
            )


if __name__ == "__main__":
    unittest.main()
