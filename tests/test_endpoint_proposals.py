from __future__ import annotations

import copy
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np

import groove_serpent.endpoint_proposals as endpoint_module
from groove_serpent.endpoint_proposals import (
    ENDPOINT_PROPOSAL_SCHEMA,
    EndpointScope,
    EndpointWindowFeature,
    analyze_endpoint_proposals,
    load_endpoint_proposal_document,
    propose_scope_endpoints,
    validate_endpoint_proposal_document,
    write_endpoint_proposal_document,
)
from groove_serpent.errors import ProjectValidationError
from groove_serpent.media import probe_audio
from groove_serpent.models import AnalysisSettings, AnalysisSummary, Project, Track
from groove_serpent.project_io import save_project
from groove_serpent.publication import canonical_json_sha256


def _synthetic_features(
    energies: list[float],
    flatness: list[float],
    *,
    sample_rate: int = 1_000,
    impulses: set[int] | None = None,
) -> tuple[EndpointWindowFeature, ...]:
    if len(energies) != len(flatness):
        raise AssertionError("Synthetic feature families must align.")
    transient_indexes = impulses or set()
    return tuple(
        EndpointWindowFeature(
            start_sample=index * sample_rate,
            end_sample_exclusive=(index + 1) * sample_rate,
            rms_dbfs=energy,
            peak_dbfs=min(0.0, energy + 10.0),
            crest_factor=3.0,
            spectral_centroid_hz=250.0 if shape < 0.8 else 100.0,
            spectral_flatness=shape,
            high_frequency_ratio=0.1,
            spectral_flux=0.0,
            derivative_peak=1.0 if index in transient_indexes else 0.01,
            impulse_count=1 if index in transient_indexes else 0,
        )
        for index, (energy, shape) in enumerate(
            zip(energies, flatness, strict=True)
        )
    )


class EndpointProposalFeatureTests(unittest.TestCase):
    def test_cross_family_proposal_preserves_quiet_tonal_intro_and_tail(self) -> None:
        energies = [-60.0] * 4 + [-53.0] * 3 + [-20.0] * 13 + [-53.0] * 4 + [-60.0] * 6
        flatness = [0.98] * 4 + [0.10] * 3 + [0.25] * 13 + [0.10] * 4 + [0.98] * 6
        features = _synthetic_features(energies, flatness, impulses={6})
        scope = EndpointScope("A", 0, 30_000)

        proposal = propose_scope_endpoints(
            scope,
            features,
            sample_rate=1_000,
        )

        self.assertEqual(proposal.status, "proposed")
        self.assertEqual(proposal.proposed_music_start_sample, 4_000)
        self.assertEqual(proposal.proposed_music_end_sample_exclusive, 24_000)
        self.assertIn("quiet_tonal_extent_protected", proposal.reasons)
        self.assertTrue(proposal.requires_review)
        self.assertTrue(
            proposal.evidence["policy"]["automatic_application_forbidden"]
        )
        self.assertTrue(
            all(
                item["role"] == "confirmation-only"
                for item in proposal.evidence["needle_confirmations"]
            )
        )

    def test_contradictory_family_boundaries_abstain_instead_of_cutting(self) -> None:
        energies = [-60.0] * 2 + [-53.0] * 8 + [-20.0] * 10 + [-60.0] * 8
        flatness = [0.98] * 2 + [0.10] * 18 + [0.98] * 8
        features = _synthetic_features(energies, flatness)

        proposal = propose_scope_endpoints(
            EndpointScope("A", 0, 28_000),
            features,
            sample_rate=1_000,
        )

        self.assertEqual(proposal.status, "abstained")
        self.assertIsNone(proposal.proposed_music_start_sample)
        self.assertEqual(proposal.reasons, ("contradictory_endpoint_families",))

    def test_silence_and_truncated_scope_abstain(self) -> None:
        silence = _synthetic_features([-90.0] * 12, [1.0] * 12)
        silent = propose_scope_endpoints(
            EndpointScope("silence", 0, 12_000),
            silence,
            sample_rate=1_000,
        )
        self.assertEqual(silent.status, "abstained")
        self.assertEqual(
            silent.reasons,
            ("silence_or_insufficient_dynamic_range",),
        )

        truncated = _synthetic_features(
            [-20.0] * 8 + [-60.0] * 4,
            [0.2] * 8 + [1.0] * 4,
        )
        clipped = propose_scope_endpoints(
            EndpointScope("truncated", 0, 12_000),
            truncated,
            sample_rate=1_000,
        )
        self.assertEqual(clipped.status, "abstained")
        self.assertEqual(
            clipped.reasons,
            ("scope_boundary_truncated_or_transition_ambiguous",),
        )

    def test_subthreshold_tonal_tail_forces_ambiguity_abstention(self) -> None:
        energies = [-60.0] * 4 + [-20.0] * 10 + [-59.0] * 2 + [-60.0] * 4
        flatness = [1.0] * 4 + [0.2] * 10 + [0.1] * 2 + [1.0] * 4

        proposal = propose_scope_endpoints(
            EndpointScope("tail", 0, 20_000),
            _synthetic_features(energies, flatness),
            sample_rate=1_000,
        )

        self.assertEqual(proposal.status, "abstained")
        self.assertEqual(
            proposal.reasons,
            ("quiet_intro_or_tail_transition_ambiguous",),
        )
        self.assertEqual(
            proposal.evidence["transition_context"][
                "quiet_tonal_after_end_samples"
            ],
            2_000,
        )

    def test_feature_windows_must_be_exact_adjacent_and_scope_bound(self) -> None:
        features = list(_synthetic_features([-60.0, -20.0, -60.0], [1.0, 0.2, 1.0]))
        features[1] = replace(features[1], start_sample=1_001)

        with self.assertRaisesRegex(ProjectValidationError, "exact, adjacent"):
            propose_scope_endpoints(
                EndpointScope("A", 0, 3_000),
                features,
                sample_rate=1_000,
            )


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and ffprobe are required",
)
class EndpointProposalIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _project(self) -> tuple[Path, Path, int]:
        sample_rate = 48_000
        duration_seconds = 14
        sample_count = sample_rate * duration_seconds
        time = np.arange(sample_count, dtype=np.float64) / sample_rate
        rng = np.random.default_rng(1_337)
        mono = rng.normal(0.0, 0.0002, sample_count)
        for start_seconds, end_seconds, frequency in (
            (1.5, 5.5, 431.0),
            (8.5, 12.5, 673.0),
        ):
            start = round(start_seconds * sample_rate)
            end = round(end_seconds * sample_rate)
            envelope = np.ones(end - start, dtype=np.float64)
            fade = sample_rate // 2
            envelope[:fade] = np.linspace(0.08, 1.0, fade)
            envelope[-fade:] = np.linspace(1.0, 0.08, fade)
            mono[start:end] += (
                0.12
                * envelope
                * np.sin(2.0 * np.pi * frequency * time[start:end])
            )
        for seconds in (1.3, 5.7, 8.3, 12.7):
            sample = round(seconds * sample_rate)
            mono[sample : sample + 2] = (0.9, -0.9)
        stereo = np.column_stack((mono, mono * 0.93))
        pcm = np.rint(np.clip(stereo, -1.0, 1.0) * 32_767.0).astype("<i2")
        source_path = self.root / "long capture.flac"
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "s16le",
                "-ar",
                str(sample_rate),
                "-ac",
                "2",
                "-i",
                "pipe:0",
                "-c:a",
                "flac",
                "-sample_fmt",
                "s16",
                str(source_path),
            ],
            input=np.ascontiguousarray(pcm).tobytes(),
            capture_output=True,
            check=True,
        )
        source = probe_audio(source_path, stored_path=source_path.name)
        project = Project(
            source=source,
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(
                music_start_seconds=0.0,
                music_end_seconds=duration_seconds,
                noise_floor_db=-72.0,
                silence_threshold_db=-66.0,
                active_threshold_db=-54.0,
                envelope_window_seconds=0.05,
            ),
            tracks=[
                Track(
                    1,
                    "Unreviewed capture",
                    0,
                    sample_count,
                    0.0,
                    float(duration_seconds),
                )
            ],
        )
        project_path = self.root / "capture.groove.json"
        save_project(project, project_path)
        return project_path, source_path, sample_rate

    @staticmethod
    def _receipts(paths: tuple[Path, ...]) -> dict[str, str]:
        return {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
        }

    def _scopes(self, sample_rate: int) -> tuple[EndpointScope, EndpointScope]:
        return (
            EndpointScope("A", 0, sample_rate * 7),
            EndpointScope("B", sample_rate * 7, sample_rate * 14),
        )

    def test_real_long_capture_uses_one_source_identity_for_two_side_scopes(self) -> None:
        project_path, source_path, sample_rate = self._project()
        before = self._receipts((project_path, source_path))

        document = analyze_endpoint_proposals(
            project_path,
            self._scopes(sample_rate),
            snapshot_workspace=self.root / "cache",
        )

        self.assertEqual(document["schema"], ENDPOINT_PROPOSAL_SCHEMA)
        self.assertEqual(before, self._receipts((project_path, source_path)))
        self.assertEqual(len(document["scopes"]), 2)
        for proposal, expected_start, expected_end in zip(
            document["scopes"],
            (round(1.5 * sample_rate), round(8.5 * sample_rate)),
            (round(5.5 * sample_rate), round(12.5 * sample_rate)),
            strict=True,
        ):
            self.assertEqual(proposal["status"], "proposed", proposal)
            self.assertLessEqual(
                abs(proposal["proposed_music_start_sample"] - expected_start),
                sample_rate // 2,
            )
            self.assertLessEqual(
                abs(proposal["proposed_music_end_sample_exclusive"] - expected_end),
                sample_rate // 2,
            )
            self.assertTrue(proposal["requires_review"])
            self.assertRegex(
                proposal["evidence"]["feature_hashes"]["combined_sha256"],
                r"^[0-9a-f]{64}$",
            )

    def test_document_is_deterministic_strict_and_never_overwrites(self) -> None:
        project_path, _source_path, sample_rate = self._project()
        document = analyze_endpoint_proposals(
            project_path,
            self._scopes(sample_rate),
            snapshot_workspace=self.root / "cache",
        )
        repeated = analyze_endpoint_proposals(
            project_path,
            self._scopes(sample_rate),
            snapshot_workspace=self.root / "cache",
        )
        self.assertEqual(document, repeated)
        destination = self.root / "endpoint proposals.json"

        receipt = write_endpoint_proposal_document(document, destination)

        self.assertEqual(load_endpoint_proposal_document(destination), document)
        self.assertEqual(receipt.sha256, hashlib.sha256(destination.read_bytes()).hexdigest())
        original = destination.read_bytes()
        with self.assertRaisesRegex(ProjectValidationError, "already exists"):
            write_endpoint_proposal_document(document, destination)
        self.assertEqual(destination.read_bytes(), original)

        raced = self.root / "raced endpoint proposal.json"

        def collide(_source: Path, target: Path) -> None:
            target.write_bytes(b"concurrent owner")
            raise FileExistsError("destination appeared")

        with mock.patch.object(
            endpoint_module,
            "rename_no_replace",
            side_effect=collide,
        ), self.assertRaisesRegex(ProjectValidationError, "appeared"):
            write_endpoint_proposal_document(document, raced)
        self.assertEqual(raced.read_bytes(), b"concurrent owner")
        self.assertEqual(tuple(self.root.glob(".raced endpoint proposal.json.*.tmp")), ())

        nul_path = Path(f"{self.root / 'nul endpoint proposal.json'}\x00ignored")
        with self.assertRaises((ValueError, OSError, ProjectValidationError)):
            write_endpoint_proposal_document(document, nul_path)
        self.assertFalse((self.root / "nul endpoint proposal.json").exists())

        duplicate = self.root / "duplicate.json"
        duplicate.write_text(
            destination.read_text(encoding="utf-8").replace(
                '"schema":',
                f'"schema": "{ENDPOINT_PROPOSAL_SCHEMA}",\n  "schema":',
                1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ProjectValidationError, "Duplicate JSON key"):
            load_endpoint_proposal_document(duplicate)

    def test_coherent_root_hash_cannot_disable_review_or_relabel_abstention(self) -> None:
        project_path, _source_path, sample_rate = self._project()
        document = analyze_endpoint_proposals(
            project_path,
            self._scopes(sample_rate),
            snapshot_workspace=self.root / "cache",
        )
        tampered = copy.deepcopy(document)
        tampered["scopes"][0]["requires_review"] = False
        without_hash = {
            key: tampered[key] for key in tampered if key != "proposal_sha256"
        }
        tampered["proposal_sha256"] = canonical_json_sha256(without_hash)

        with self.assertRaisesRegex(ProjectValidationError, "requires human review"):
            validate_endpoint_proposal_document(tampered)

    def test_snapshot_substitution_is_detected_before_a_proposal_can_publish(self) -> None:
        project_path, _source_path, sample_rate = self._project()
        real_decode = endpoint_module._decode_scope_features

        def substitute_snapshot(*args, **kwargs):
            snapshot_path = Path(args[0])
            features = real_decode(*args, **kwargs)
            moved = snapshot_path.with_name("original.flac")
            snapshot_path.rename(moved)
            snapshot_path.write_bytes(b"substituted")
            return features

        with mock.patch.object(
            endpoint_module,
            "_decode_scope_features",
            side_effect=substitute_snapshot,
        ):
            with self.assertRaisesRegex(
                ProjectValidationError,
                "snapshot changed",
            ):
                analyze_endpoint_proposals(
                    project_path,
                    (self._scopes(sample_rate)[0],),
                    snapshot_workspace=self.root / "cache",
                )

    def test_live_source_toctou_is_detected_even_after_snapshot_decode(self) -> None:
        project_path, source_path, sample_rate = self._project()
        real_decode = endpoint_module._decode_scope_features

        def mutate_live_source(*args, **kwargs):
            features = real_decode(*args, **kwargs)
            source_path.write_bytes(source_path.read_bytes() + b"changed")
            return features

        with mock.patch.object(
            endpoint_module,
            "_decode_scope_features",
            side_effect=mutate_live_source,
        ):
            with self.assertRaisesRegex(
                ProjectValidationError,
                "changed during the verified audio operation",
            ):
                analyze_endpoint_proposals(
                    project_path,
                    (self._scopes(sample_rate)[0],),
                    snapshot_workspace=self.root / "cache",
                )


if __name__ == "__main__":
    unittest.main()
