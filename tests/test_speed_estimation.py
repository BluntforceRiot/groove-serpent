from __future__ import annotations

import json
import io
import math
import os
import tempfile
import unittest
from copy import deepcopy
from contextlib import redirect_stdout
from hashlib import sha256
from pathlib import Path
from typing import Any
from unittest.mock import patch

from groove_serpent.errors import ProjectValidationError
from groove_serpent.cli import main
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.project_io import load_project_with_sha256, save_project
from groove_serpent.publication import canonical_json_sha256
from groove_serpent.speed_estimation import (
    BOUNDARY_REVIEW_SCHEMA,
    DURATION_PROVENANCE_SCHEMA,
    REFERENCE_TRACKLIST_SCHEMA,
    SpeedEstimatorConfig,
    create_boundary_review_evidence,
    estimate_speed,
    load_boundary_review_evidence,
    load_speed_proposal,
    load_speed_reference_tracklist,
    project_track_ranges_sha256,
    speed_proposal_bytes,
    write_speed_proposal,
)


SAMPLE_RATE = 48_000
REFERENCE_DURATIONS = [100.0, 120.0, 140.0, 160.0, 180.0, 200.0]
SIDES = ["A", "A", "A", "B", "B", "B"]


def _write_case(
    root: Path,
    factors: list[float],
    *,
    reference_durations: list[float] | None = None,
    reference_titles: list[str] | None = None,
    project_sides: list[str] | None = None,
    reference_sides: list[str] | None = None,
) -> tuple[Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    durations = reference_durations or REFERENCE_DURATIONS[: len(factors)]
    titles = [f"Track {index}" for index in range(1, len(factors) + 1)]
    chosen_reference_titles = reference_titles or titles
    chosen_project_sides = project_sides or SIDES[: len(factors)]
    chosen_reference_sides = reference_sides or SIDES[: len(factors)]
    tracks: list[Track] = []
    cursor = 0
    for index, (duration, factor) in enumerate(
        zip(durations, factors, strict=True), start=1
    ):
        samples = round(duration / factor * SAMPLE_RATE)
        end = cursor + samples
        tracks.append(
            Track(
                number=index,
                title=titles[index - 1],
                start_sample=cursor,
                end_sample=end,
                start_seconds=cursor / SAMPLE_RATE,
                end_seconds=end / SAMPLE_RATE,
                confidence=0.99,
                side=chosen_project_sides[index - 1],
            )
        )
        cursor = end
    source_bytes = b"Groove Serpent synthetic speed-estimation source\n"
    source_path = root / "capture.flac"
    source_path.write_bytes(source_bytes)
    source_stat = source_path.stat()
    project = Project(
        source=AudioSource(
            path="capture.flac",
            filename="capture.flac",
            size_bytes=len(source_bytes),
            modified_ns=source_stat.st_mtime_ns,
            duration_seconds=cursor / SAMPLE_RATE,
            sample_rate=SAMPLE_RATE,
            channels=2,
            codec_name="flac",
            bits_per_raw_sample=24,
            sample_format="s32",
            sample_count=cursor,
            sha256=sha256(source_bytes).hexdigest(),
        ),
        settings=AnalysisSettings(min_track_seconds=1.0),
        analysis=AnalysisSummary(
            music_start_seconds=0.0,
            music_end_seconds=cursor / SAMPLE_RATE,
            noise_floor_db=-50.0,
            silence_threshold_db=-44.0,
            active_threshold_db=-32.0,
            envelope_window_seconds=0.05,
        ),
        tracks=tracks,
        metadata={"artist": "Synthetic", "album": "Speed Ground Truth"},
    )
    project.validate()
    project_path = root / "synthetic.groove.json"
    save_project(project, project_path)
    reference_path = root / "reference.tracklist.json"
    reference_path.write_text(
        json.dumps(
            {
                "schema": REFERENCE_TRACKLIST_SCHEMA,
                "artist": "Synthetic",
                "album": "Speed Ground Truth",
                "duration_provenance": {
                    "schema": DURATION_PROVENANCE_SCHEMA,
                    "source_description": "Independent synthetic ground truth",
                    "independent_of_project_boundaries": True,
                },
                "tracks": [
                    {
                        "number": index,
                        "title": chosen_reference_titles[index - 1],
                        "side": chosen_reference_sides[index - 1],
                        "duration": durations[index - 1],
                    }
                    for index in range(1, len(factors) + 1)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded, project_sha256 = load_project_with_sha256(project_path)
    boundary_review_path = root / "boundary-review.json"
    boundary_review_path.write_text(
        json.dumps(
            {
                "schema": BOUNDARY_REVIEW_SCHEMA,
                "project_sha256": project_sha256,
                "project_revision": loaded.revision,
                "project_state_sha256": loaded.state_sha256,
                "source_sha256": loaded.source.sha256,
                "track_ranges_sha256": project_track_ranges_sha256(loaded),
                "reviewed_at": "2026-07-13T00:00:00+00:00",
                "review_method": "audio-and-visual-boundary-review",
                "all_track_boundaries_reviewed": True,
                "reviewed_boundaries_independent_of_reference_durations": True,
                "correction_approval": "not-granted",
            }
        ),
        encoding="utf-8",
    )
    return project_path, reference_path, boundary_review_path


def _estimate_case(
    project_path: Path, reference_path: Path, boundary_review_path: Path
) -> dict[str, Any]:
    return estimate_speed(
        project_path,
        reference_path,
        boundary_review_path=boundary_review_path,
    )


class SpeedEstimationTests(unittest.TestCase):
    def test_ground_truth_small_delta_is_proposed_and_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path, reference_path, boundary_review_path = _write_case(
                root, [1.04] * 6
            )
            before = project_path.read_bytes()
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )

            self.assertEqual(proposal["estimate"]["status"], "proposed")
            self.assertEqual(proposal["estimate"]["confidence"], "high")
            self.assertAlmostEqual(proposal["estimate"]["proposed_factor"], 1.04, places=7)
            self.assertEqual(proposal["diagnostics"]["usable_track_count"], 6)
            self.assertEqual(proposal["diagnostics"]["independent_side_count"], 2)
            self.assertFalse(proposal["authority"]["may_apply_correction"])
            self.assertFalse(proposal["authority"]["may_change_project"])
            self.assertEqual(proposal["authority"]["human_approval"], "not-inferred")
            self.assertEqual(project_path.read_bytes(), before)
            loaded, _project_sha256 = load_project_with_sha256(project_path)
            self.assertEqual(proposal["source"]["sha256"], loaded.source.sha256)
            self.assertEqual(len(proposal["project"]["sha256"]), 64)
            self.assertEqual(len(proposal["reference_tracklist"]["raw_sha256"]), 64)
            self.assertEqual(len(proposal["tool"]["sha256"]), 64)
            self.assertEqual(len(proposal["config"]["sha256"]), 64)
            hypotheses = proposal["estimate"]["rpm_hypotheses"]
            self.assertEqual(hypotheses[0]["nominal_pair"], "same-nominal-speed-unspecified")
            self.assertAlmostEqual(hypotheses[0]["coarse_factor"], 1.0)
            self.assertAlmostEqual(hypotheses[0]["fine_factor"], 1.04, places=7)
            self.assertEqual(
                hypotheses[0]["authority"], "hypothesis-only-not-inferred"
            )

    def test_circular_duration_agreement_without_review_evidence_abstains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path, reference_path, _boundary_review_path = _write_case(
                Path(directory), [1.0] * 6
            )
            proposal = estimate_speed(project_path, reference_path)
            self.assertAlmostEqual(
                proposal["estimate"]["diagnostic_center_factor"], 1.0
            )
            self.assertEqual(proposal["estimate"]["status"], "abstained")
            self.assertIsNone(proposal["estimate"]["proposed_factor"])
            self.assertEqual(
                proposal["diagnostics"]["boundary_review_status"], "missing"
            )
            self.assertIn(
                "boundary_review_evidence_missing",
                proposal["diagnostics"]["abstention_reasons"],
            )

    def test_duration_independence_must_be_explicit_and_true(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path, reference_path, boundary_review_path = _write_case(
                root, [1.01] * 6
            )
            payload = json.loads(reference_path.read_text(encoding="utf-8"))
            del payload["duration_provenance"]
            reference_path.write_text(json.dumps(payload), encoding="utf-8")
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertEqual(proposal["estimate"]["status"], "abstained")
            self.assertIn(
                "reference_duration_independence_unconfirmed",
                proposal["diagnostics"]["abstention_reasons"],
            )

            payload["duration_provenance"] = {
                "schema": DURATION_PROVENANCE_SCHEMA,
                "source_description": "Durations used to fit the markers",
                "independent_of_project_boundaries": False,
            }
            reference_path.write_text(json.dumps(payload), encoding="utf-8")
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertIn(
                "reference_durations_not_independent",
                proposal["diagnostics"]["abstention_reasons"],
            )

    def test_stale_or_inadequate_boundary_review_evidence_abstains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path, reference_path, boundary_review_path = _write_case(
                root, [1.02] * 6
            )
            receipt = json.loads(boundary_review_path.read_text(encoding="utf-8"))
            receipt["project_sha256"] = "b" * 64
            boundary_review_path.write_text(json.dumps(receipt), encoding="utf-8")
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertEqual(
                proposal["diagnostics"]["boundary_review_status"], "stale"
            )
            self.assertIn(
                "boundary_review_evidence_stale",
                proposal["diagnostics"]["abstention_reasons"],
            )

            loaded, project_sha256 = load_project_with_sha256(project_path)
            receipt["project_sha256"] = project_sha256
            receipt["all_track_boundaries_reviewed"] = False
            boundary_review_path.write_text(json.dumps(receipt), encoding="utf-8")
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertEqual(
                proposal["diagnostics"]["boundary_review_status"], "inadequate"
            )
            self.assertIsNone(proposal["estimate"]["proposed_factor"])
            self.assertEqual(loaded.state_sha256, receipt["project_state_sha256"])

    def test_boundary_review_receipt_cannot_grant_correction_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path, reference_path, boundary_review_path = _write_case(
                Path(directory), [1.01] * 6
            )
            receipt = json.loads(boundary_review_path.read_text(encoding="utf-8"))
            receipt["correction_approval"] = "granted"
            boundary_review_path.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(ProjectValidationError, "cannot grant"):
                _estimate_case(project_path, reference_path, boundary_review_path)

    def test_coarse_shellac_ratio_within_supported_range_is_proposed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            factor = (100.0 / 3.0) / 78.26
            project_path, reference_path, boundary_review_path = _write_case(
                Path(directory), [factor] * 6
            )
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertEqual(proposal["estimate"]["status"], "proposed")
            self.assertAlmostEqual(
                proposal["estimate"]["proposed_factor"], factor, places=8
            )
            self.assertGreaterEqual(proposal["estimate"]["proposed_factor"], 0.25)
            hypothesis = proposal["estimate"]["rpm_hypotheses"][0]
            self.assertAlmostEqual(hypothesis["capture_rpm"], 100.0 / 3.0)
            self.assertAlmostEqual(hypothesis["intended_rpm"], 78.26)
            self.assertAlmostEqual(hypothesis["fine_factor"], 1.0, places=7)

    def test_45_over_33_rpm_hypothesis_is_decomposed_from_fine_factor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path, reference_path, boundary_review_path = _write_case(
                Path(directory), [1.35] * 6
            )
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertEqual(proposal["estimate"]["status"], "proposed")
            hypothesis = proposal["estimate"]["rpm_hypotheses"][0]
            self.assertAlmostEqual(hypothesis["capture_rpm"], 45.0)
            self.assertAlmostEqual(hypothesis["intended_rpm"], 100.0 / 3.0)
            self.assertAlmostEqual(hypothesis["coarse_factor"], 1.35)
            self.assertAlmostEqual(hypothesis["fine_factor"], 1.0, places=7)

    def test_one_contaminated_reference_is_excluded_robustly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path, reference_path, boundary_review_path = _write_case(
                Path(directory), [1.04, 1.04, 0.70, 1.04, 1.04, 1.04]
            )
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertEqual(proposal["estimate"]["status"], "proposed")
            self.assertAlmostEqual(proposal["estimate"]["proposed_factor"], 1.04, places=7)
            excluded = [
                row for row in proposal["tracks"] if row["disposition"] == "excluded"
            ]
            self.assertEqual(len(excluded), 1)
            self.assertEqual(excluded[0]["exclusion_reason"], "robust_log_outlier")

    def test_quiet_short_track_is_documented_and_does_not_dominate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            durations = [20.0, 100.0, 120.0, 140.0, 160.0, 180.0]
            project_path, reference_path, boundary_review_path = _write_case(
                Path(directory),
                [0.5, 1.03, 1.03, 1.03, 1.03, 1.03],
                reference_durations=durations,
            )
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertEqual(proposal["estimate"]["status"], "proposed")
            self.assertAlmostEqual(proposal["estimate"]["proposed_factor"], 1.03, places=7)
            self.assertEqual(
                proposal["tracks"][0]["exclusion_reason"],
                "reference_duration_too_short",
            )

    def test_missing_or_non_numeric_duration_is_rejected_by_strict_loader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reference.json"
            base = {"tracks": [{"title": "One", "side": "A", "duration": 100.0}]}
            for mutation in (
                lambda value: value["tracks"][0].pop("duration"),
                lambda value: value["tracks"][0].__setitem__("duration", "100"),
                lambda value: value["tracks"][0].__setitem__("duration", True),
            ):
                payload = deepcopy(base)
                mutation(payload)
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(payload=payload), self.assertRaises(
                    ProjectValidationError
                ):
                    load_speed_reference_tracklist(path)

    def test_duplicate_and_unexpected_reference_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reference.json"
            path.write_text(
                '{"tracks":[],"tracks":[]}', encoding="utf-8"
            )
            with self.assertRaisesRegex(ProjectValidationError, "Duplicate"):
                load_speed_reference_tracklist(path)
            path.write_text(
                json.dumps(
                    {
                        "artist": "Synthetic",
                        "album": "Speed Ground Truth",
                        "tracks": [
                            {
                                "title": "One",
                                "side": "A",
                                "duration": 100,
                                "guess": 1.1,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProjectValidationError, "unexpected"):
                load_speed_reference_tracklist(path)

    def test_mismatched_title_and_side_force_abstention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            titles = [f"Track {index}" for index in range(1, 7)]
            titles[2] = "Different edition"
            project_path, reference_path, boundary_review_path = _write_case(
                root, [1.04] * 6, reference_titles=titles
            )
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertEqual(proposal["estimate"]["status"], "abstained")
            self.assertIsNone(proposal["estimate"]["proposed_factor"])
            self.assertIn(
                "reference_identity_mismatch",
                proposal["diagnostics"]["abstention_reasons"],
            )

            project_path, reference_path, boundary_review_path = _write_case(
                root / "second",
                [1.04] * 6,
                reference_sides=["A", "A", "C", "B", "B", "B"],
            )
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertEqual(proposal["estimate"]["status"], "abstained")

    def test_release_metadata_mismatch_forces_abstention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path, reference_path, boundary_review_path = _write_case(
                Path(directory), [1.04] * 6
            )
            payload = json.loads(reference_path.read_text(encoding="utf-8"))
            payload["album"] = "Different Release"
            reference_path.write_text(json.dumps(payload), encoding="utf-8")
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertEqual(proposal["estimate"]["status"], "abstained")
            self.assertEqual(
                proposal["diagnostics"]["release_identity_status"], "mismatch"
            )
            self.assertIn(
                "release_metadata_mismatch",
                proposal["diagnostics"]["abstention_reasons"],
            )

    def test_count_mismatch_and_insufficient_evidence_abstain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path, reference_path, boundary_review_path = _write_case(
                root, [1.02] * 3
            )
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertEqual(proposal["estimate"]["status"], "abstained")
            self.assertIn(
                "insufficient_usable_tracks",
                proposal["diagnostics"]["abstention_reasons"],
            )

            payload = json.loads(reference_path.read_text(encoding="utf-8"))
            payload["tracks"].pop()
            reference_path.write_text(json.dumps(payload), encoding="utf-8")
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertIn(
                "track_count_mismatch",
                proposal["diagnostics"]["abstention_reasons"],
            )

    def test_ambiguous_bimodal_and_side_disagreement_abstain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path, reference_path, boundary_review_path = _write_case(
                Path(directory), [1.0, 1.0, 1.0, 1.1, 1.1, 1.1]
            )
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertEqual(proposal["estimate"]["status"], "abstained")
            reasons = proposal["diagnostics"]["abstention_reasons"]
            self.assertIn("track_ratios_inconsistent", reasons)
            self.assertIn("side_estimates_disagree", reasons)

    def test_extreme_ratio_outside_derivative_range_abstains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_path, reference_path, boundary_review_path = _write_case(
                Path(directory), [2.1] * 6
            )
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertEqual(proposal["estimate"]["status"], "abstained")
            self.assertIn(
                "factor_outside_supported_range",
                proposal["diagnostics"]["abstention_reasons"],
            )
            self.assertIsNone(proposal["estimate"]["proposed_factor"])

    def test_one_underrepresented_side_abstains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sides = ["A", "A", "A", "A", "A", "B"]
            project_path, reference_path, boundary_review_path = _write_case(
                Path(directory),
                [1.01] * 6,
                project_sides=sides,
                reference_sides=sides,
            )
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertEqual(proposal["estimate"]["status"], "abstained")
            self.assertIn(
                "insufficient_independent_tracks_per_side",
                proposal["diagnostics"]["abstention_reasons"],
            )

    def test_deterministic_bytes_round_trip_and_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path, reference_path, boundary_review_path = _write_case(
                root, [1.04] * 6
            )
            first = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            second = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            self.assertEqual(first, second)
            self.assertEqual(speed_proposal_bytes(first), speed_proposal_bytes(second))

            output = root / "proposal.json"
            proposal_sha256 = write_speed_proposal(first, output)
            self.assertEqual(load_speed_proposal(output), first)
            self.assertEqual(proposal_sha256, first["proposal_sha256"])
            with self.assertRaisesRegex(ProjectValidationError, "already exists"):
                write_speed_proposal(first, output)

            tampered = json.loads(output.read_text(encoding="utf-8"))
            tampered["estimate"]["diagnostic_center_factor"] = 1.5
            del tampered["proposal_sha256"]
            tampered["proposal_sha256"] = canonical_json_sha256(tampered)
            output.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ProjectValidationError, "evidence|seal"):
                load_speed_proposal(output)

    def test_output_race_never_deletes_competing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path, reference_path, boundary_review_path = _write_case(
                root, [1.0] * 6
            )
            proposal = _estimate_case(
                project_path, reference_path, boundary_review_path
            )
            output = root / "proposal.json"

            def race(_source: os.PathLike[str], destination: os.PathLike[str]) -> None:
                Path(destination).write_text("competitor", encoding="utf-8")
                raise FileExistsError("race")

            with patch(
                "groove_serpent.speed_estimation.rename_no_replace",
                side_effect=race,
            ):
                with self.assertRaises(FileExistsError):
                    write_speed_proposal(proposal, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "competitor")

    def test_invalid_config_is_rejected(self) -> None:
        with self.assertRaises(ProjectValidationError):
            SpeedEstimatorConfig(supported_factor_minimum=0.1).validate()
        with self.assertRaises(ProjectValidationError):
            SpeedEstimatorConfig(outlier_mad_multiplier=math.inf).validate()

    def test_boundary_review_creator_requires_explicit_attestations_and_live_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path, _reference_path, _existing_review = _write_case(
                root, [1.02] * 6
            )
            output = root / "created-boundary-review.json"
            with self.assertRaisesRegex(
                ProjectValidationError, "explicit confirmation"
            ):
                create_boundary_review_evidence(
                    project_path,
                    output,
                    confirm_all_track_boundaries_reviewed=False,
                    confirm_review_independent_of_reference_durations=True,
                )
            evidence = create_boundary_review_evidence(
                project_path,
                output,
                confirm_all_track_boundaries_reviewed=True,
                confirm_review_independent_of_reference_durations=True,
                reviewed_at="2026-07-13T12:00:00+00:00",
            )
            self.assertEqual(evidence.correction_approval, "not-granted")
            self.assertTrue(evidence.all_track_boundaries_reviewed)
            self.assertEqual(
                load_boundary_review_evidence(output).canonical_sha256,
                evidence.canonical_sha256,
            )
            with self.assertRaisesRegex(ProjectValidationError, "already exists"):
                create_boundary_review_evidence(
                    project_path,
                    output,
                    confirm_all_track_boundaries_reviewed=True,
                    confirm_review_independent_of_reference_durations=True,
                )

            (root / "capture.flac").write_bytes(b"tampered source")
            with self.assertRaisesRegex(
                ProjectValidationError, "does not match the project source identity"
            ):
                estimate_speed(
                    project_path,
                    root / "reference.tracklist.json",
                    boundary_review_path=output,
                )

    def test_cli_creates_non_approving_boundary_review_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path, _reference_path, _existing_review = _write_case(
                root, [1.025] * 6
            )
            output = root / "owner-review.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = main(
                    [
                        "speed",
                        "review-boundaries",
                        str(project_path),
                        "--output",
                        str(output),
                        "--confirm-all-boundaries-reviewed",
                        "--confirm-review-independent-of-reference-durations",
                    ]
                )
            self.assertEqual(result, 0)
            evidence = load_boundary_review_evidence(output)
            self.assertEqual(evidence.correction_approval, "not-granted")
            self.assertIn("approval remains not granted", stdout.getvalue())

    def test_cli_writes_and_emits_same_non_mutating_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path, reference_path, boundary_review_path = _write_case(
                root, [1.025] * 6
            )
            project_before = project_path.read_bytes()
            output_path = root / "speed.proposal.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = main(
                    [
                        "speed",
                        "estimate",
                        str(project_path),
                        "--tracklist",
                        str(reference_path),
                        "--boundary-review",
                        str(boundary_review_path),
                        "--output",
                        str(output_path),
                        "--json",
                    ]
                )
            self.assertEqual(result, 0)
            emitted = json.loads(stdout.getvalue())
            self.assertEqual(emitted, load_speed_proposal(output_path))
            self.assertEqual(emitted["estimate"]["status"], "proposed")
            self.assertEqual(project_path.read_bytes(), project_before)


if __name__ == "__main__":
    unittest.main()
