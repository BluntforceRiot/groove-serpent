from __future__ import annotations

import copy
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np

from groove_serpent.continuous_preview_workflow import (
    CONTINUOUS_METHOD_REGISTRY,
    CONTINUOUS_REVIEW_ACKNOWLEDGEMENT,
    ReviewedNoiseReference,
    continuous_attestation_template,
    current_continuous_preview_context,
    discover_continuous_preview_catalog,
    find_current_continuous_artifact,
    load_continuous_preview_receipt,
    propose_continuous_preview,
    reject_continuous_proposal,
    render_continuous_preview,
    validate_continuous_attestation,
    validate_continuous_preview_receipt,
)
from groove_serpent.errors import ProjectValidationError
from groove_serpent.media import probe_audio, sha256_file
from groove_serpent.models import AnalysisSettings, AnalysisSummary, Project, Track
from groove_serpent.project_io import load_project, save_project
from groove_serpent.publication import canonical_json_sha256


def _hum_capture(sample_rate: int = 8_000, seconds: int = 28) -> np.ndarray:
    count = sample_rate * seconds
    time = np.arange(count, dtype=np.float64) / sample_rate
    samples = np.random.default_rng(44).normal(0.0, 0.00002, (count, 2))
    hum = (
        0.002 * np.sin(2.0 * np.pi * 60.0 * time)
        + 0.0008 * np.sin(2.0 * np.pi * 120.0 * time)
        + 0.0004 * np.sin(2.0 * np.pi * 180.0 * time)
    )
    samples += hum[:, np.newaxis]
    program = slice(6 * sample_rate, 22 * sample_rate)
    samples[program] += (
        0.04 * np.sin(2.0 * np.pi * 440.0 * time[program])
    )[:, np.newaxis]
    return samples


def _crackle_capture(sample_rate: int = 8_000, seconds: int = 4) -> np.ndarray:
    count = sample_rate * seconds
    time = np.arange(count, dtype=np.float64) / sample_rate
    envelope = 0.65 + 0.25 * np.sin(2.0 * np.pi * 0.31 * time)
    mono = envelope * (
        0.028 * np.sin(2.0 * np.pi * 440.0 * time)
        + 0.011 * np.sin(2.0 * np.pi * 997.0 * time)
    )
    samples = np.column_stack((mono, mono * 0.93))
    samples += np.random.default_rng(713).normal(0.0, 0.00001, samples.shape)
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
        samples[position, 0] += amplitude
        samples[position, 1] += amplitude * 0.91
    return samples


def _write_flac(path: Path, pcm: np.ndarray, sample_rate: int) -> None:
    framed = np.ascontiguousarray(pcm, dtype="<f8")
    completed = subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "f64le",
            "-ar",
            str(sample_rate),
            "-ac",
            str(framed.shape[1]),
            "-i",
            "pipe:0",
            "-c:a",
            "flac",
            "-sample_fmt",
            "s32",
            str(path),
        ],
        input=framed.tobytes(order="C"),
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))


def _project_for(source: Path) -> Path:
    audio = probe_audio(source, stored_path=source.name)
    assert audio.sample_count is not None
    project = Project(
        source=audio,
        settings=AnalysisSettings(min_track_seconds=0.1),
        analysis=AnalysisSummary(
            music_start_seconds=0.0,
            music_end_seconds=audio.duration_seconds,
            noise_floor_db=-70.0,
            silence_threshold_db=-64.0,
            active_threshold_db=-42.0,
            envelope_window_seconds=0.05,
        ),
        tracks=[
            Track(
                number=1,
                title="Synthetic",
                start_sample=0,
                end_sample=audio.sample_count,
                start_seconds=0.0,
                end_seconds=audio.sample_count / audio.sample_rate,
                confidence=1.0,
            )
        ],
    )
    project_path = source.with_suffix(".groove.json")
    save_project(project, project_path)
    return project_path


def _references(sample_rate: int) -> tuple[ReviewedNoiseReference, ...]:
    return (
        ReviewedNoiseReference("lead-in", "lead_in", 2 * sample_rate, 6 * sample_rate, True),
        ReviewedNoiseReference(
            "lead-out", "lead_out", 22 * sample_rate, 26 * sample_rate, True
        ),
    )


class ContinuousPreviewWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "synthetic.flac"
        self.sample_rate = 8_000
        _write_flac(self.source, _hum_capture(), self.sample_rate)
        self.project = _project_for(self.source)
        self.source_sha256 = sha256_file(self.source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _propose(self) -> tuple[Path, dict[str, Any]]:
        context = current_continuous_preview_context(self.project, "hum")
        output, proposal = propose_continuous_preview(
            self.project,
            kind="hum",
            start_sample=0,
            end_sample_exclusive=28 * self.sample_rate,
            references=_references(self.sample_rate),
            expected_context=context,
        )
        return output, proposal

    def _attestation(self, proposal: dict[str, Any]) -> dict[str, Any]:
        attestation = continuous_attestation_template(proposal)
        attestation["attestation_token"] = hashlib.sha256(b"owner-audition-request").hexdigest()
        return attestation

    def test_registry_has_four_distinct_exact_method_contracts(self) -> None:
        self.assertEqual(
            set(CONTINUOUS_METHOD_REGISTRY),
            {"hum", "rumble", "hiss", "crackle"},
        )
        profiles = {
            contract.authority_profile for contract in CONTINUOUS_METHOD_REGISTRY.values()
        }
        self.assertEqual(len(profiles), 4)
        for name, contract in CONTINUOUS_METHOD_REGISTRY.items():
            with self.subTest(name=name):
                self.assertIn(name, contract.authority_profile)
                self.assertTrue(contract.proposal_schema)
                self.assertTrue(contract.recipe_schema)
                self.assertTrue(contract.render_schema)
                self.assertTrue(contract.receipt_schema)

    def test_propose_render_and_reopen_exact_crackle_preview(self) -> None:
        source = self.root / "crackle.flac"
        _write_flac(source, _crackle_capture(), self.sample_rate)
        project = _project_for(source)
        source_sha256 = sha256_file(source)
        before_project = project.read_bytes()
        references = (
            ReviewedNoiseReference("lead-in", "lead_in", 1_600, 6_400, True),
            ReviewedNoiseReference("lead-out", "lead_out", 25_600, 30_400, True),
        )
        context = current_continuous_preview_context(project, "crackle")
        _proposal_path, proposal = propose_continuous_preview(
            project,
            kind="crackle",
            start_sample=0,
            end_sample_exclusive=32_000,
            references=references,
            expected_context=context,
        )
        self.assertEqual(proposal["status"], "proposed")
        self.assertEqual(
            proposal["authority"]["method_profile"],
            "bounded_continuous_crackle_owner_audition_only",
        )
        self.assertGreaterEqual(
            proposal["foundation"]["metrics"]["stored_event_count"],
            8,
        )

        bundle, receipt = render_continuous_preview(
            project,
            proposal,
            self._attestation(proposal),
        )

        self.assertEqual(project.read_bytes(), before_project)
        self.assertEqual(sha256_file(source), source_sha256)
        self.assertEqual(load_continuous_preview_receipt(bundle), receipt)
        self.assertEqual(validate_continuous_preview_receipt(receipt), receipt)
        self.assertGreater(
            receipt["foundation_receipt"]["metrics"]["changed_sample_values"],
            0,
        )
        self.assertEqual(
            receipt["foundation_receipt"]["metrics"][
                "outside_event_changed_sample_values"
            ],
            0,
        )

    def test_propose_render_rediscover_and_reopen_exact_hum_preview(self) -> None:
        before_project = self.project.read_bytes()
        proposal_path, proposal = self._propose()
        self.assertEqual(proposal["status"], "proposed")
        self.assertEqual(
            proposal["authority"]["method_profile"],
            "stationary_hum_owner_audition_only",
        )
        self.assertTrue(proposal_path.is_file())
        attestation = self._attestation(proposal)

        bundle, receipt = render_continuous_preview(
            self.project, proposal, attestation
        )

        self.assertEqual(self.project.read_bytes(), before_project)
        self.assertEqual(sha256_file(self.source), self.source_sha256)
        self.assertEqual(load_continuous_preview_receipt(bundle), receipt)
        self.assertEqual(validate_continuous_preview_receipt(receipt), receipt)
        self.assertEqual(
            set(path.name for path in bundle.iterdir()),
            {"preview.json", "original.wav", "proposed.wav", "removed.wav"},
        )
        self.assertEqual(
            {receipt["audio"][role]["audition_gain"] for role in receipt["audio"]},
            {
                receipt["foundation_receipt"]["audition"]["original_linear_gain"],
                receipt["foundation_receipt"]["audition"]["proposed_linear_gain"],
                receipt["foundation_receipt"]["audition"]["residue_monitor_linear_gain"],
            },
        )
        catalog = discover_continuous_preview_catalog(self.project)
        self.assertEqual(catalog["summary"]["current"], 2)
        reopened = find_current_continuous_artifact(
            self.project,
            artifact_kind="preview",
            identity_sha256=receipt["receipt_sha256"],
        )
        self.assertEqual(reopened["payload"], receipt)

    def test_explicit_rejection_is_persistent_and_non_mutating(self) -> None:
        before_project = self.project.read_bytes()
        _proposal_path, proposal = self._propose()
        decision_path, decision = reject_continuous_proposal(
            self.project,
            proposal,
            reason="Owner heard audible damage in the proposed comparison.",
        )
        self.assertTrue(decision_path.is_file())
        self.assertEqual(decision["decision"], "reject_proposal_without_applying")
        self.assertEqual(self.project.read_bytes(), before_project)
        self.assertEqual(sha256_file(self.source), self.source_sha256)
        reopened = find_current_continuous_artifact(
            self.project,
            artifact_kind="decision",
            identity_sha256=decision["decision_sha256"],
        )
        self.assertEqual(reopened["payload"], decision)

    def test_attestation_requires_every_exact_expected_hash(self) -> None:
        _path, proposal = self._propose()
        valid = self._attestation(proposal)
        self.assertEqual(validate_continuous_attestation(valid, proposal), valid)
        expected = valid["expected"]
        assert isinstance(expected, dict)
        for key in tuple(expected):
            with self.subTest(key=key):
                changed = copy.deepcopy(valid)
                changed["expected"][key] = "0" * 64
                with self.assertRaisesRegex(ProjectValidationError, "Expected project"):
                    validate_continuous_attestation(changed, proposal)
        for key in (
            "owner_attested_scope_reviewed",
            "owner_attested_references_reviewed",
        ):
            changed = copy.deepcopy(valid)
            changed[key] = False
            with self.assertRaises(ProjectValidationError):
                validate_continuous_attestation(changed, proposal)
        changed = copy.deepcopy(valid)
        changed["acknowledgement"] = CONTINUOUS_REVIEW_ACKNOWLEDGEMENT + "-forged"
        with self.assertRaises(ProjectValidationError):
            validate_continuous_attestation(changed, proposal)

    def test_resource_reference_and_context_races_fail_closed(self) -> None:
        context = current_continuous_preview_context(self.project, "hum")
        with self.assertRaisesRegex(ProjectValidationError, "stale"):
            propose_continuous_preview(
                self.project,
                kind="hum",
                start_sample=0,
                end_sample_exclusive=28 * self.sample_rate,
                references=_references(self.sample_rate),
                expected_context={
                    **context,
                    "limits": {
                        **context["limits"],
                        "maximum_scope_seconds": 10.0,
                    },
                },
            )
        with patch(
            "groove_serpent.continuous_preview_workflow.MAX_SCOPE_SECONDS",
            10.0,
        ):
            constrained_context = current_continuous_preview_context(
                self.project, "hum"
            )
            with self.assertRaisesRegex(ProjectValidationError, "10-second"):
                propose_continuous_preview(
                    self.project,
                    kind="hum",
                    start_sample=0,
                    end_sample_exclusive=28 * self.sample_rate,
                    references=_references(self.sample_rate),
                    expected_context=constrained_context,
                )
        invalid_refs = list(_references(self.sample_rate))
        invalid_refs[0] = ReviewedNoiseReference(
            "lead-in", "lead_in", 2 * self.sample_rate, 6 * self.sample_rate, False
        )
        with self.assertRaisesRegex(ProjectValidationError, "explicit owner"):
            propose_continuous_preview(
                self.project,
                kind="hum",
                start_sample=0,
                end_sample_exclusive=28 * self.sample_rate,
                references=invalid_refs,
                expected_context=context,
            )

    def test_catalog_marks_prior_artifacts_stale_after_speed_state_change(self) -> None:
        _path, proposal = self._propose()
        project = load_project(self.project)
        project.metadata.update(
            {
                "speed_capture_rpm": "33.333333333333336",
                "speed_intended_rpm": "33.333333333333336",
                "speed_fine_factor": "1.001",
            }
        )
        save_project(project, self.project)
        catalog = discover_continuous_preview_catalog(self.project)
        self.assertEqual(catalog["summary"]["stale"], 1)
        with self.assertRaisesRegex(ProjectValidationError, "No unique current"):
            find_current_continuous_artifact(
                self.project,
                artifact_kind="proposal",
                identity_sha256=proposal["proposal_sha256"],
            )

    def test_receipt_tampering_fails_even_when_outer_hash_is_recomputed(self) -> None:
        _path, proposal = self._propose()
        bundle, receipt = render_continuous_preview(
            self.project, proposal, self._attestation(proposal)
        )
        self.assertTrue(bundle.is_dir())
        changed = copy.deepcopy(receipt)
        changed["authority"]["automatic_application_forbidden"] = False
        body = dict(changed)
        del body["receipt_sha256"]
        changed["receipt_sha256"] = canonical_json_sha256(body)
        with self.assertRaises(ProjectValidationError):
            validate_continuous_preview_receipt(changed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
