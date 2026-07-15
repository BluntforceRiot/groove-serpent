from __future__ import annotations

import copy
import hashlib
import unittest

import numpy as np

from groove_serpent.continuous_noise import NoiseAnalysisScope, NoiseReferenceRegion
from groove_serpent.crackle_preview import (
    CRACKLE_PREVIEW_RECEIPT_SCHEMA,
    CRACKLE_PREVIEW_RECIPE_SCHEMA,
    CRACKLE_PREVIEW_RENDER_SCHEMA,
    CRACKLE_PROPOSAL_SCHEMA,
    CRACKLE_REVIEW_ATTESTATION_SCHEMA,
    REVIEW_ACKNOWLEDGEMENT,
    CrackleAnalysisConfig,
    CracklePreviewConfig,
    CracklePreviewRecipe,
    CrackleProposal,
    analyze_crackle,
    create_crackle_preview_recipe,
    render_crackle_preview,
    validate_crackle_preview_receipt,
    validate_crackle_preview_render_manifest,
    validate_crackle_proposal,
)
from groove_serpent.errors import ProjectValidationError
from groove_serpent.publication import canonical_json_sha256


SAMPLE_RATE = 8_000
SECONDS = 4
FRAME_COUNT = SAMPLE_RATE * SECONDS


def _clean_program() -> np.ndarray:
    time = np.arange(FRAME_COUNT, dtype=np.float64) / SAMPLE_RATE
    envelope = 0.65 + 0.25 * np.sin(2.0 * np.pi * 0.31 * time)
    mono = envelope * (
        0.028 * np.sin(2.0 * np.pi * 440.0 * time)
        + 0.011 * np.sin(2.0 * np.pi * 997.0 * time)
    )
    stereo = np.column_stack((mono, mono * 0.93))
    stereo += np.random.default_rng(713).normal(0.0, 0.00001, stereo.shape)
    return stereo


def _crackled_program() -> tuple[np.ndarray, tuple[int, ...]]:
    audio = _clean_program()
    positions = (
        2_800,
        4_300,
        6_000,
        9_400,
        11_300,
        13_700,
        16_400,
        19_200,
        21_800,
        25_700,
        27_600,
        29_400,
    )
    for index, position in enumerate(positions):
        amplitude = 0.42 if index % 2 == 0 else -0.38
        audio[position, 0] += amplitude
        audio[position, 1] += amplitude * 0.91
    return audio, positions


def _scope() -> NoiseAnalysisScope:
    return NoiseAnalysisScope("reviewed_scope", 0, FRAME_COUNT)


def _references() -> tuple[NoiseReferenceRegion, ...]:
    return (
        NoiseReferenceRegion("lead-in", "lead_in", 1_600, 6_400),
        NoiseReferenceRegion("lead-out", "lead_out", 25_600, 30_400),
    )


def _proposal(audio: np.ndarray | None = None) -> CrackleProposal:
    source = audio if audio is not None else _crackled_program()[0]
    return analyze_crackle(
        source,
        sample_rate=SAMPLE_RATE,
        scope=_scope(),
        noise_references=_references(),
    )


def _attestation(proposal: CrackleProposal) -> dict[str, object]:
    return {
        "schema": CRACKLE_REVIEW_ATTESTATION_SCHEMA,
        "attestation_token": hashlib.sha256(
            b"distinct-crackle-owner-audition-request"
        ).hexdigest(),
        "decision": "request_owner_audition_preview",
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "selected_scope": proposal.scope,
        "acknowledgement": REVIEW_ACKNOWLEDGEMENT,
    }


class CracklePreviewTests(unittest.TestCase):
    def test_detects_bounded_synthetic_crackle_and_binds_reference_evidence(self) -> None:
        proposal = _proposal()
        payload = proposal.to_dict()
        self.assertEqual(payload["schema"], CRACKLE_PROPOSAL_SCHEMA)
        self.assertEqual(proposal.status, "proposed")
        self.assertGreaterEqual(payload["metrics"]["stored_event_count"], 8)
        self.assertGreaterEqual(payload["metrics"]["reference_event_count"], 2)
        self.assertLess(
            payload["metrics"]["repaired_sample_value_fraction"],
            payload["config"]["maximum_repaired_fraction"],
        )
        self.assertTrue(payload["authority"]["owner_audition_required"])
        self.assertFalse(payload["authority"]["may_modify_source_audio"])

    def test_clean_music_and_smooth_broad_transient_abstain(self) -> None:
        clean = _clean_program()
        start = SAMPLE_RATE + 300
        width = 400
        window = np.hanning(width)
        clean[start : start + width, 0] += 0.18 * window
        clean[start : start + width, 1] += 0.16 * window
        proposal = _proposal(clean)
        self.assertEqual(proposal.status, "abstained")
        self.assertIn(
            "insufficient_conservative_crackle_events",
            proposal.to_dict()["abstention_reasons"],
        )

    def test_preview_changes_only_listed_sample_channels_and_residue_reconstructs(self) -> None:
        audio, _positions = _crackled_program()
        proposal = _proposal(audio)
        recipe = create_crackle_preview_recipe(proposal, _attestation(proposal))
        result = render_crackle_preview(audio, proposal, recipe)
        mask = np.zeros(audio.shape, dtype=np.bool_)
        for event in proposal.events:
            for channel in event["channels"]:
                mask[
                    event["start_sample"] : event["end_sample_exclusive"],
                    channel,
                ] = True
        self.assertTrue(np.array_equal(result.proposed[~mask], result.original[~mask]))
        np.testing.assert_allclose(
            result.proposed + result.removed,
            result.original,
            rtol=0.0,
            atol=1e-12,
        )
        self.assertGreater(np.count_nonzero(result.removed[mask]), 0)
        self.assertEqual(
            result.render_manifest["metrics"]["outside_event_changed_sample_values"],
            0,
        )
        self.assertTrue(
            result.receipt["audition"]["matched_loudness_is_not_quality_approval"]
        )

    def test_distinct_schema_chain_and_round_trip(self) -> None:
        proposal = _proposal()
        recipe = create_crackle_preview_recipe(proposal, _attestation(proposal))
        result = render_crackle_preview(_crackled_program()[0], proposal, recipe)
        self.assertEqual(recipe.to_dict()["schema"], CRACKLE_PREVIEW_RECIPE_SCHEMA)
        self.assertEqual(
            result.render_manifest["schema"], CRACKLE_PREVIEW_RENDER_SCHEMA
        )
        self.assertEqual(result.receipt["schema"], CRACKLE_PREVIEW_RECEIPT_SCHEMA)
        self.assertEqual(
            CrackleProposal.from_dict(proposal.to_dict()).to_dict(),
            proposal.to_dict(),
        )
        self.assertEqual(
            CracklePreviewRecipe.from_dict(recipe.to_dict()).to_dict(),
            recipe.to_dict(),
        )
        validate_crackle_preview_render_manifest(
            result.render_manifest,
            recipe=recipe,
        )
        validate_crackle_preview_receipt(
            result.receipt,
            recipe=recipe,
            render_manifest=result.render_manifest,
        )

    def test_tampered_proposal_fails_even_with_recomputed_outer_hash(self) -> None:
        payload = _proposal().to_dict()
        payload["events"][0]["confidence"] = 0.0
        body = dict(payload)
        del body["proposal_body_sha256"]
        payload["proposal_body_sha256"] = canonical_json_sha256(body)
        with self.assertRaises(ProjectValidationError):
            validate_crackle_proposal(payload)

    def test_stale_or_forged_attestation_cannot_create_recipe(self) -> None:
        proposal = _proposal()
        attestation = _attestation(proposal)
        attestation["proposal_body_sha256"] = "0" * 64
        with self.assertRaisesRegex(ProjectValidationError, "another proposal"):
            create_crackle_preview_recipe(proposal, attestation)
        attestation = _attestation(proposal)
        attestation["acknowledgement"] = "human approved"
        with self.assertRaisesRegex(ProjectValidationError, "acknowledgement"):
            create_crackle_preview_recipe(proposal, attestation)

    def test_abstained_proposal_cannot_render(self) -> None:
        proposal = _proposal(_clean_program())
        with self.assertRaisesRegex(ProjectValidationError, "abstained"):
            create_crackle_preview_recipe(proposal, _attestation(proposal))

    def test_reference_assertions_and_config_are_strict(self) -> None:
        audio, _positions = _crackled_program()
        with self.assertRaisesRegex(ProjectValidationError, "2-64"):
            analyze_crackle(
                audio,
                sample_rate=SAMPLE_RATE,
                scope=_scope(),
                noise_references=_references()[:1],
            )
        with self.assertRaises(ProjectValidationError):
            CrackleAnalysisConfig(local_window_samples=64).validate()
        with self.assertRaises(ProjectValidationError):
            CracklePreviewConfig(lpc_order=True).validate()

    def test_repaired_fraction_limit_forces_abstention(self) -> None:
        proposal = analyze_crackle(
            _crackled_program()[0],
            sample_rate=SAMPLE_RATE,
            scope=_scope(),
            noise_references=_references(),
            config=CrackleAnalysisConfig(maximum_repaired_fraction=0.000001),
        )
        self.assertEqual(proposal.status, "abstained")
        self.assertIn(
            "proposed_repair_fraction_exceeds_conservative_limit",
            proposal.to_dict()["abstention_reasons"],
        )

    def test_receipt_authority_tampering_fails(self) -> None:
        audio, _positions = _crackled_program()
        proposal = _proposal(audio)
        recipe = create_crackle_preview_recipe(proposal, _attestation(proposal))
        result = render_crackle_preview(audio, proposal, recipe)
        changed = copy.deepcopy(result.receipt)
        changed["authority"]["automatic_application_forbidden"] = False
        body = dict(changed)
        del body["receipt_sha256"]
        changed["receipt_sha256"] = canonical_json_sha256(body)
        with self.assertRaisesRegex(ProjectValidationError, "authority"):
            validate_crackle_preview_receipt(
                changed,
                recipe=recipe,
                render_manifest=result.render_manifest,
            )


if __name__ == "__main__":
    unittest.main()
