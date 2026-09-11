from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import patch

import test_continuous_preview_workflow as fixture_module
from groove_serpent import continuous_preview_workflow as continuous
from groove_serpent.audio_snapshot import verified_audio_snapshot
from groove_serpent.errors import ProjectValidationError
from groove_serpent.media import probe_audio, sha256_file
from groove_serpent.models import AnalysisSettings, AnalysisSummary, Project, Track
from groove_serpent.project_io import load_project, save_project


class ContinuousDescriptorAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture_module.ContinuousPreviewWorkflowTests()
        self.fixture.setUp()
        self.actual = probe_audio(self.fixture.source)

    def tearDown(self) -> None:
        self.assertEqual(sha256_file(self.fixture.source), self.fixture.source_sha256)
        self.fixture.tearDown()

    def _claim(self, **changes):
        source = replace(self.actual, path=self.fixture.source.name, **changes)
        end = min(self.actual.sample_count, source.sample_count)
        end_seconds = end / source.sample_rate
        project = Project(
            source=source,
            revision=load_project(self.fixture.project).revision,
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(
                music_start_seconds=0.0, music_end_seconds=end_seconds,
                noise_floor_db=-70.0, silence_threshold_db=-64.0, active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=[Track(1, "Synthetic descriptor authority", 0, end, 0.0, end_seconds)],
        )
        save_project(project, self.fixture.project)

    def _propose(self, **options):
        return continuous.propose_continuous_preview(
            self.fixture.project, kind="hum", start_sample=0,
            end_sample_exclusive=self.actual.sample_count,
            references=fixture_module._references(self.actual.sample_rate), **options,
        )

    def test_proposal_refuses_real_audio_descriptor_mismatches(self) -> None:
        cases = (
            {"channels": 1},
            {"sample_rate": self.actual.sample_rate * 2,
             "sample_count": self.actual.sample_count * 2},
            {"sample_count": self.actual.sample_count + 1},
            {"bits_per_raw_sample": 16},
            {"sample_format": "s16"},
            {"codec_name": "pcm_s24le"},
            {"duration_seconds": self.actual.duration_seconds + 1.0,
             "sample_count": self.actual.sample_count + self.actual.sample_rate},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self._claim(**changes)
                project_before = self.fixture.project.read_bytes()
                with self.assertRaisesRegex(ProjectValidationError, "descriptor"):
                    self._propose()
                self.assertEqual(self.fixture.project.read_bytes(), project_before)

    def test_snapshot_probe_precedes_decode_and_uses_snapshot_not_origin(self) -> None:
        self._claim(channels=1)
        snapshot = verified_audio_snapshot(
            self.fixture.source, expected_sha256=self.actual.sha256,
            expected_size_bytes=self.actual.size_bytes,
        )
        try:
            with (
                patch.object(continuous, "probe_audio", wraps=probe_audio) as observed,
                patch.object(
                    continuous, "_decode_scope",
                    side_effect=AssertionError("Decoded false geometry"),
                ),
            ):
                with self.assertRaisesRegex(ProjectValidationError, "descriptor.*channels"):
                    self._propose(source_snapshot=snapshot)
            self.assertEqual(observed.call_count, 1)
            self.assertEqual(observed.call_args.args[0], snapshot.path)
            self.assertNotEqual(snapshot.path, self.fixture.source)
            snapshot.assert_snapshot_unchanged(force=True)
        finally:
            snapshot.close()

    def test_render_rejects_legacy_mono_proposal_for_real_stereo_source(self) -> None:
        self._claim(channels=1)
        # Construct the pre-fix artifact using its historical missing comparison.
        # The render under test below has no descriptor or audio-probe mocks.
        with patch.object(
            continuous, "audio_source_descriptor_mismatches", return_value=(), create=True
        ):
            _, proposal = self._propose()
        self.assertEqual(proposal["status"], "proposed")
        self.assertEqual(proposal["foundation"]["channel_count"], 1)
        project_before = self.fixture.project.read_bytes()
        with self.assertRaisesRegex(ProjectValidationError, "descriptor.*channels"):
            continuous.render_continuous_preview(
                self.fixture.project, proposal, self.fixture._attestation(proposal)
            )
        self.assertEqual(self.fixture.project.read_bytes(), project_before)
        self.assertEqual(
            list(continuous._workspace_for(self.fixture.project).glob("preview-*")), []
        )
