from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np

from groove_serpent import restoration_workflow, review_server
from groove_serpent.audio_snapshot import verified_audio_snapshot
from groove_serpent.errors import GrooveSerpentError, ProjectValidationError
from groove_serpent.media import probe_audio
from groove_serpent.models import AnalysisSettings, AnalysisSummary, Project, Track
from groove_serpent.project_io import load_project, save_project


def _native_project(
    directory: Path,
    *,
    channels: int = 2,
    changes: dict[str, Any] | None = None,
) -> tuple[Project, Path, Path]:
    source_path = directory / "native side.flac"
    time = np.arange(16_000, dtype=np.float64) / 8_000
    signals = [0.1 * np.sin(2 * np.pi * (211 + index * 96) * time)
               for index in range(channels)]
    pcm = np.rint(np.column_stack(signals) * 32_767).astype("<i2")
    subprocess.run(
        [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error", "-n",
         "-f", "s16le", "-ar", "8000", "-ac", str(channels), "-i", "pipe:0",
         "-c:a", "flac", "-sample_fmt", "s16", str(source_path)],
        input=pcm.tobytes(), capture_output=True, check=True,
    )
    source = replace(probe_audio(source_path, stored_path=source_path.name), **(changes or {}))
    assert source.sample_count is not None
    project = Project(
        source=source,
        settings=AnalysisSettings(min_track_seconds=0.1),
        analysis=AnalysisSummary(0.0, source.duration_seconds, -60, -54, -42, 0.05),
        tracks=[Track(1, "Native", 0, source.sample_count, 0.0, source.duration_seconds)],
    )
    project_path = directory / "native side.groove.json"
    save_project(project, project_path)
    return project, project_path, source_path


def _false_descriptors() -> tuple[dict[str, Any], ...]:
    return (
        {"channels": 1},
        {"sample_rate": 16_000, "duration_seconds": 1.0},
        {"sample_count": 8_000, "duration_seconds": 1.0},
        {"codec_name": "pcm_s16le"},
        {"bits_per_raw_sample": 24},
        {"bits_per_raw_sample": None},
        {"sample_format": "s32"},
        {"sample_format": None},
    )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class ReviewRestorationDescriptorAuthorityTests(unittest.TestCase):
    def test_review_rejects_descriptor_edits_after_successful_startup(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project, project_path, source_path = _native_project(Path(value))
            source_before = source_path.read_bytes()
            server = review_server.ReviewServer(("127.0.0.1", 0), project_path)
            try:
                project.source.channels = 1
                save_project(project, project_path)
                current = load_project(project_path)
                project_before = project_path.read_bytes()
                for operation in (
                    server.verify_source,
                    server.verified_source_snapshot,
                    server.open_playback_snapshot,
                    server._seed_source_verification_cache,
                ):
                    with self.subTest(operation=operation.__name__):
                        with self.assertRaisesRegex(ProjectValidationError, "descriptor.*channels"):
                            operation(current)
                self.assertEqual(project_before, project_path.read_bytes())
                self.assertEqual(source_before, source_path.read_bytes())
            finally:
                server.server_close()

    def test_matching_review_leases_need_no_new_probe_or_full_source_read(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project, project_path, _ = _native_project(Path(value))
            server = review_server.ReviewServer(("127.0.0.1", 0), project_path)
            try:
                with (
                    mock.patch.object(
                        review_server, "probe_audio", side_effect=AssertionError("Reprobed source"),
                    ),
                    mock.patch.object(
                        review_server, "_sha256_handle",
                        side_effect=AssertionError("Reread whole source"),
                    ),
                ):
                    for _ in range(3):
                        snapshot, _ = server.verified_source_snapshot(
                            project, force_full=False, evidence_lease=True,
                        )
                        self.assertEqual(snapshot.path, server.source_snapshot.path)
                        _, handle, _ = server.open_playback_snapshot(project)
                        handle.close()
            finally:
                server.server_close()

    def test_startup_descriptor_cache_does_not_alias_probed_result(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            project, project_path, _ = _native_project(Path(value))
            observed = []

            def observe(path: Path, stored_path: str | None = None):
                actual = probe_audio(path, stored_path=stored_path)
                observed.append(actual)
                return actual

            with mock.patch.object(review_server, "probe_audio", side_effect=observe):
                server = review_server.ReviewServer(("127.0.0.1", 0), project_path)
            try:
                self.assertEqual(len(observed), 1)
                self.assertIsNot(server._verified_source_descriptor, observed[0])
                observed[0].bits_per_raw_sample = 24
                project.source.bits_per_raw_sample = 24
                save_project(project, project_path)
                with self.assertRaisesRegex(ProjectValidationError, "descriptor.*bits"):
                    server.verified_source_snapshot(load_project(project_path))
            finally:
                server.server_close()

    def test_review_startup_rejects_false_descriptors_before_catalog_work(self) -> None:
        for changes in _false_descriptors():
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as value:
                _, project_path, source_path = _native_project(Path(value), changes=changes)
                originals = (source_path.read_bytes(), project_path.read_bytes())
                server = None
                with mock.patch.object(
                    review_server, "discover_restoration_catalog",
                    wraps=review_server.discover_restoration_catalog,
                ) as catalog:
                    try:
                        with self.assertRaisesRegex(ProjectValidationError, "descriptor"):
                            server = review_server.ReviewServer(("127.0.0.1", 0), project_path)
                    finally:
                        if server is not None:
                            server.server_close()
                    catalog.assert_not_called()
                self.assertEqual(originals, (source_path.read_bytes(), project_path.read_bytes()))

    def test_restoration_rejects_false_descriptors_before_scan_work(self) -> None:
        for changes in _false_descriptors():
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as value:
                directory = Path(value)
                _, project_path, source_path = _native_project(directory, changes=changes)
                originals = (source_path.read_bytes(), project_path.read_bytes())
                report_path = directory / "scan.json"
                with mock.patch.object(
                    restoration_workflow, "_scan_project_clicks",
                    wraps=restoration_workflow._scan_project_clicks,
                ) as scan:
                    with self.assertRaisesRegex(GrooveSerpentError, "no longer matches"):
                        restoration_workflow.scan_project_clicks(project_path, report_path)
                    scan.assert_not_called()
                self.assertFalse(report_path.exists())
                self.assertEqual(originals, (source_path.read_bytes(), project_path.read_bytes()))

    def test_mono_source_cannot_be_declared_stereo(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            _, project_path, source_path = _native_project(
                directory, channels=1, changes={"channels": 2},
            )
            originals = (source_path.read_bytes(), project_path.read_bytes())
            server = None
            try:
                with self.assertRaisesRegex(ProjectValidationError, "descriptor"):
                    server = review_server.ReviewServer(("127.0.0.1", 0), project_path)
            finally:
                if server is not None:
                    server.server_close()
            with self.assertRaisesRegex(GrooveSerpentError, "no longer matches"):
                restoration_workflow.scan_project_clicks(project_path, directory / "scan.json")
            self.assertFalse((directory / "scan.json").exists())
            self.assertEqual(originals, (source_path.read_bytes(), project_path.read_bytes()))

    def test_native_mono_and_stereo_probe_only_verified_snapshots(self) -> None:
        for channels in (1, 2):
            with self.subTest(channels=channels), tempfile.TemporaryDirectory() as value:
                directory = Path(value)
                project, project_path, source_path = _native_project(directory, channels=channels)
                originals = (source_path.read_bytes(), project_path.read_bytes())
                # Spies delegate to the actual FFprobe implementation. These native
                # checks do not substitute synthetic descriptors or fake audio bytes.
                with mock.patch.object(review_server, "probe_audio", wraps=probe_audio) as probe:
                    server = review_server.ReviewServer(("127.0.0.1", 0), project_path)
                    try:
                        probe.assert_called_once_with(
                            server.source_snapshot.path, stored_path=source_path.name,
                        )
                        self.assertNotEqual(server.source_snapshot.path, source_path)
                    finally:
                        snapshot_path = server.source_snapshot.path
                        server.server_close()
                    self.assertFalse(snapshot_path.exists())
                with verified_audio_snapshot(
                    source_path, expected_sha256=project.source.sha256,
                    expected_size_bytes=project.source.size_bytes,
                    workspace=directory / "snapshots", label="Source audio",
                ) as snapshot:
                    with mock.patch.object(
                        restoration_workflow, "probe_audio", wraps=probe_audio,
                    ) as probe:
                        restoration_workflow.scan_project_clicks(
                            project_path, directory / "scan.json", source_snapshot=snapshot,
                        )
                        probe.assert_called_once_with(snapshot.path, stored_path=source_path.name)
                        self.assertNotEqual(snapshot.path, source_path)
                self.assertTrue((directory / "scan.json").is_file())
                self.assertEqual(originals, (source_path.read_bytes(), project_path.read_bytes()))

    def test_restoration_validates_duration_of_an_existing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            project, project_path, source_path = _native_project(directory)
            originals = (source_path.read_bytes(), project_path.read_bytes())
            with verified_audio_snapshot(
                source_path, expected_sha256=project.source.sha256,
                expected_size_bytes=project.source.size_bytes,
                workspace=directory / "snapshots", label="Source audio",
            ) as snapshot:
                project.source.duration_seconds += 0.1
                with self.assertRaisesRegex(GrooveSerpentError, "no longer matches"):
                    restoration_workflow._validated_source(project_path, project, snapshot)
            self.assertEqual(originals, (source_path.read_bytes(), project_path.read_bytes()))
