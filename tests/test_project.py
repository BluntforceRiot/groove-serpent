from __future__ import annotations

import json
import math
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import numpy as np
import groove_serpent.project_io as project_io_module

from groove_serpent.models import (
    EDIT_ACTION_KINDS,
    MAX_CHECKPOINTS,
    MAX_EDIT_HISTORY,
    AnalyzerBaseline,
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    BoundaryCandidate,
    EditHistoryEntry,
    Project,
    ProjectState,
    Track,
    _source_path_kind,
    project_state_sha256,
    resolve_source_path,
)
from groove_serpent.project_io import load_project, save_project
from groove_serpent.errors import ProjectValidationError


def make_history_project() -> Project:
    project = Project(
        source=AudioSource(
            path="side.flac",
            filename="side.flac",
            size_bytes=12_345,
            modified_ns=123,
            duration_seconds=10.0,
            sample_rate=1_000,
            channels=2,
            codec_name="flac",
            sample_count=10_000,
            sha256="a" * 64,
        ),
        settings=AnalysisSettings(min_track_seconds=0.1),
        analysis=AnalysisSummary(0.0, 10.0, -50.0, -44.0, -32.0, 0.05),
        tracks=[
            Track(1, "One", 0, 5_000, 0.0, 5.0, artist="Artist"),
            Track(2, "Two", 5_000, 10_000, 5.0, 10.0, artist="Artist"),
        ],
        metadata={"artist": "Artist", "album": "Album"},
    )
    project.validate()
    return project


class ProjectTests(unittest.TestCase):
    def test_source_path_syntax_rejects_windows_network_device_and_drive_relative(self) -> None:
        project = make_history_project()
        invalid = (
            r"\\server\share\capture.flac",
            "//server/share/capture.flac",
            r"\\?\N:\capture.flac",
            r"\\?\UNC\server\share\capture.flac",
            r"\\.\PhysicalDrive0",
            r"\??\N:\capture.flac",
            r"\Device\HarddiskVolume1\capture.flac",
            r"N:capture.flac",
            "N:",
            "NUL.flac",
            r"captures\COM1.wav",
            "capture.flac:$DATA",
            r"N:\capture.flac:stream",
        )
        for stored in invalid:
            with self.subTest(stored=stored):
                project.source.path = stored
                with patch("pathlib.Path.resolve") as resolve, patch(
                    "pathlib.Path.is_file"
                ) as is_file, self.assertRaises(ProjectValidationError):
                    resolve_source_path(project, Path("project.groove.json"))
                resolve.assert_not_called()
                is_file.assert_not_called()

        project.source.path = "missing.flac"
        project.source.filename = "NUL.flac"
        with patch("pathlib.Path.resolve") as resolve, patch(
            "pathlib.Path.is_file"
        ) as is_file, self.assertRaisesRegex(
            ProjectValidationError, "device-name"
        ):
            resolve_source_path(project, Path("project.groove.json"))
        resolve.assert_not_called()
        is_file.assert_not_called()

    def test_source_path_syntax_preserves_mapped_drives_and_relative_paths(self) -> None:
        self.assertEqual(
            _source_path_kind(r"N:\Vinyl\capture.flac"), "windows-absolute"
        )
        self.assertEqual(
            _source_path_kind("N:/Vinyl/capture.flac"), "windows-absolute"
        )
        self.assertEqual(_source_path_kind("captures/side.flac"), "relative")

        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source = directory / "capture.flac"
            source.write_bytes(b"audio")
            project = make_history_project()
            project.source.path = str(source)
            project.source.filename = source.name
            self.assertEqual(
                resolve_source_path(project, directory / "side.groove.json"),
                source,
            )

    def test_round_trip(self) -> None:
        source = AudioSource(
            path="side-a.flac",
            filename="side-a.flac",
            size_bytes=1234,
            modified_ns=1,
            duration_seconds=10.0,
            sample_rate=1_000,
            channels=2,
            codec_name="flac",
            bits_per_raw_sample=24,
            sample_format="s32",
        )
        project = Project(
            source=source,
            settings=AnalysisSettings(),
            analysis=AnalysisSummary(
                music_start_seconds=1.0,
                music_end_seconds=9.0,
                noise_floor_db=-50.0,
                silence_threshold_db=-44.0,
                active_threshold_db=-32.0,
                envelope_window_seconds=0.05,
            ),
            tracks=[
                Track(
                    number=1,
                    title="First",
                    start_sample=1_000,
                    end_sample=5_000,
                    start_seconds=1.0,
                    end_seconds=5.0,
                ),
                Track(
                    number=2,
                    title="Second",
                    start_sample=5_000,
                    end_sample=9_000,
                    start_seconds=5.0,
                    end_seconds=9.0,
                ),
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.groove.json"
            save_project(project, path)
            loaded = load_project(path)
            self.assertEqual(loaded.source.bits_per_raw_sample, 24)
            self.assertEqual(loaded.tracks[1].title, "Second")
            self.assertEqual(loaded.tracks[0].end_sample, loaded.tracks[1].start_sample)

    def test_schema_one_project_requires_explicit_migration_without_writes(self) -> None:
        source = AudioSource(
            path="side.flac",
            filename="side.flac",
            size_bytes=1,
            modified_ns=1,
            duration_seconds=1.0,
            sample_rate=1_000,
            channels=2,
            codec_name="flac",
        )
        project = Project(
            source=source,
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(0.0, 1.0, -50.0, -44.0, -32.0, 0.05),
            tracks=[Track(1, "One", 0, 1_000, 0.0, 1.0)],
        )
        payload = project.to_dict()
        payload["schema_version"] = 1
        with tempfile.TemporaryDirectory() as directory_value:
            path = Path(directory_value) / "old.groove.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            original = path.read_bytes()
            with self.assertRaisesRegex(ProjectValidationError, "project migrate PROJECT"):
                load_project(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_successful_overwrites_increment_revision_even_in_the_same_second(self) -> None:
        source = AudioSource(
            path="side.flac",
            filename="side.flac",
            size_bytes=1,
            modified_ns=1,
            duration_seconds=1.0,
            sample_rate=1_000,
            channels=2,
            codec_name="flac",
        )
        project = Project(
            source=source,
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(0.0, 1.0, -50.0, -44.0, -32.0, 0.05),
            tracks=[Track(1, "One", 0, 1_000, 0.0, 1.0)],
        )
        with (
            tempfile.TemporaryDirectory() as directory_value,
            patch(
                "groove_serpent.project_io.utc_now_iso", return_value="2026-07-11T12:00:00+00:00"
            ),
        ):
            path = Path(directory_value) / "revision.groove.json"
            save_project(project, path)
            first_revision = project.revision
            save_project(project, path)
            second_revision = project.revision
            save_project(project, path)
            loaded = load_project(path)
        self.assertEqual((first_revision, second_revision, loaded.revision), (1, 2, 3))
        self.assertEqual(loaded.updated_at, "2026-07-11T12:00:00+00:00")

    def test_failed_atomic_replace_does_not_increment_revision(self) -> None:
        source = AudioSource(
            path="side.flac",
            filename="side.flac",
            size_bytes=1,
            modified_ns=1,
            duration_seconds=1.0,
            sample_rate=1_000,
            channels=2,
            codec_name="flac",
        )
        project = Project(
            source=source,
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(0.0, 1.0, -50.0, -44.0, -32.0, 0.05),
            tracks=[Track(1, "One", 0, 1_000, 0.0, 1.0)],
        )
        with tempfile.TemporaryDirectory() as directory_value:
            path = Path(directory_value) / "revision.groove.json"
            save_project(project, path)
            original_bytes = path.read_bytes()
            original_revision = project.revision
            original_updated_at = project.updated_at
            with patch("groove_serpent.project_io.os.replace", side_effect=OSError("no replace")):
                with self.assertRaises(OSError):
                    save_project(project, path)
            self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(project.revision, original_revision)
        self.assertEqual(project.updated_at, original_updated_at)

    def test_non_finite_settings_are_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                settings = AnalysisSettings(threshold_margin_db=value)
                with self.assertRaises(ProjectValidationError):
                    settings.validate()

    def test_extreme_json_integers_raise_validation_errors_not_overflow(self) -> None:
        huge = 10**400
        with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
            AnalysisSettings(min_gap_seconds=huge).validate()
        with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
            AnalysisSummary(0.0, huge, -50.0, -44.0, -32.0, 0.05).validate()

        candidate = BoundaryCandidate(
            4.0,
            5.0,
            4.5,
            4_500,
            huge,
            -60.0,
            -55.0,
            12.0,
            0.8,
        )
        with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
            candidate.validate()

        project = make_history_project()
        project.source.duration_seconds = huge
        with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
            project.validate()
        project.source.duration_seconds = 10.0
        project.tracks[0].confidence = huge
        with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
            project.validate()

    def test_finite_huge_values_fail_stably_in_models_and_project_files(self) -> None:
        mutations = (
            lambda project: setattr(project.source, "duration_seconds", 1e308),
            lambda project: setattr(project.analysis, "music_end_seconds", 1e308),
            lambda project: setattr(project.analysis, "envelope_window_seconds", 1e308),
            lambda project: setattr(project.settings, "active_run_seconds", 1e308),
            lambda project: setattr(project.settings, "threshold_margin_db", 1e308),
            lambda project: setattr(project.analysis, "noise_floor_db", 1e308),
            lambda project: setattr(project.analysis, "waveform", [1e308]),
            lambda project: setattr(project.tracks[0], "expected_duration_seconds", 1e308),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                project = make_history_project()
                mutation(project)
                with self.assertRaises(ProjectValidationError):
                    project.validate()

                payload = make_history_project().to_dict()
                loaded_shape = Project.from_dict(payload)
                mutation(loaded_shape)
                payload = loaded_shape.to_dict()
                with tempfile.TemporaryDirectory() as directory_value:
                    path = Path(directory_value) / "finite-huge.groove.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ProjectValidationError):
                        load_project(path)

    def test_analysis_geometry_is_bound_to_source_and_exact_cut_samples(self) -> None:
        invalid_music_bounds = (
            (-0.001, 10.0),
            (5.0, 5.0),
            (0.0, 10.001),
        )
        for start, end in invalid_music_bounds:
            with self.subTest(start=start, end=end):
                project = make_history_project()
                project.analysis.music_start_seconds = start
                project.analysis.music_end_seconds = end
                with self.assertRaisesRegex(ProjectValidationError, "music bounds"):
                    project.validate()

        project = make_history_project()
        project.analysis.candidates = [
            BoundaryCandidate(
                start_seconds=4.0,
                end_seconds=5.0,
                cut_seconds=4.5,
                cut_sample=9_000,
                duration_seconds=1.0,
                minimum_db=-60.0,
                mean_db=-55.0,
                contrast_db=12.0,
                score=0.8,
            )
        ]
        with self.assertRaisesRegex(ProjectValidationError, "time and sample disagree"):
            project.validate()

        project = make_history_project()
        project.analysis.candidates = [
            BoundaryCandidate(
                start_seconds=9.5,
                end_seconds=10.5,
                cut_seconds=10.0,
                cut_sample=10_000,
                duration_seconds=1.0,
                minimum_db=-60.0,
                mean_db=-55.0,
                contrast_db=12.0,
                score=0.8,
            )
        ]
        with self.assertRaisesRegex(ProjectValidationError, "within the source"):
            project.validate()

    def test_candidate_order_and_duration_geometry_are_validated(self) -> None:
        candidate = BoundaryCandidate(
            start_seconds=4.0,
            end_seconds=5.0,
            cut_seconds=5.1,
            cut_sample=5_100,
            duration_seconds=1.0,
            minimum_db=-60.0,
            mean_db=-55.0,
            contrast_db=12.0,
            score=0.8,
        )
        with self.assertRaisesRegex(ProjectValidationError, "invalid bounds"):
            candidate.validate()

        project = make_history_project()
        project.analysis.candidates = [
            BoundaryCandidate(
                start_seconds=4.0,
                end_seconds=5.0,
                cut_seconds=4.5,
                cut_sample=4_500,
                duration_seconds=2.0,
                minimum_db=-60.0,
                mean_db=-55.0,
                contrast_db=12.0,
                score=0.8,
            )
        ]
        with self.assertRaisesRegex(ProjectValidationError, "duration disagrees"):
            project.validate()

    def test_analysis_levels_contrast_and_waveform_have_domain_bounds(self) -> None:
        for field_name in (
            "minimum_db",
            "mean_db",
            "contrast_db",
        ):
            candidate = BoundaryCandidate(
                4.0,
                5.0,
                4.5,
                4_500,
                1.0,
                -60.0,
                -55.0,
                12.0,
                0.8,
            )
            setattr(candidate, field_name, 1e308)
            with (
                self.subTest(field=field_name),
                self.assertRaisesRegex(ProjectValidationError, "supported dB range"),
            ):
                candidate.validate()

        candidate = BoundaryCandidate(4.0, 5.0, 4.5, 4_500, 1.0, -60.0, -55.0, -0.001, 0.8)
        with self.assertRaisesRegex(ProjectValidationError, "contrast"):
            candidate.validate()

        for waveform in ([-0.001], [1.001], [1e308]):
            project = make_history_project()
            project.analysis.waveform = waveform
            with (
                self.subTest(waveform=waveform),
                self.assertRaisesRegex(ProjectValidationError, "between 0 and 1"),
            ):
                project.validate()

    def test_candidate_cut_tolerance_is_half_a_source_sample(self) -> None:
        project = make_history_project()
        project.analysis.candidates = [
            BoundaryCandidate(
                4.0,
                5.0,
                4.5005,
                4_500,
                1.0,
                -60.0,
                -55.0,
                12.0,
                0.8,
            )
        ]
        project.validate()

        project.analysis.candidates[0].cut_seconds = 4.5006
        with self.assertRaisesRegex(ProjectValidationError, "half a source sample"):
            project.validate()

        project = make_history_project()
        project.analysis.candidates = [
            BoundaryCandidate(
                9.0,
                10.0,
                10.0,
                10_001,
                1.0,
                -60.0,
                -55.0,
                12.0,
                0.8,
            )
        ]
        with self.assertRaisesRegex(ProjectValidationError, "cut sample.*outside"):
            project.validate()

    def test_checkpoint_names_are_unicode_normalization_insensitive(self) -> None:
        project = make_history_project()
        project.set_checkpoint("Caf\u00e9")
        project.set_checkpoint("Cafe\u0301")
        self.assertEqual(len(project.checkpoints), 1)
        self.assertEqual(
            project.checkpoint_state("Caf\u00e9").sha256,
            project.state_sha256,
        )

        duplicate = deepcopy(project.checkpoints[0])
        duplicate.name = "Caf\u00e9"
        project.checkpoints.append(duplicate)
        with self.assertRaisesRegex(ProjectValidationError, "names must be unique"):
            project.validate()

    def test_non_standard_json_numbers_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            path = Path(directory_value) / "bad.groove.json"
            path.write_text('{"schema_version": NaN}', encoding="utf-8")
            with self.assertRaisesRegex(ProjectValidationError, "Invalid JSON number"):
                load_project(path)

            path.write_text('{"schema_version": 1e400}', encoding="utf-8")
            with self.assertRaises(ProjectValidationError):
                load_project(path)

    def test_integer_analysis_settings_reject_fractional_and_boolean_values(self) -> None:
        for field_name, value in (
            ("analysis_rate", 8_000.5),
            ("window_ms", True),
            ("smoothing_windows", 1.5),
            ("waveform_points", 4_000.5),
        ):
            with self.subTest(field=field_name):
                settings = AnalysisSettings()
                setattr(settings, field_name, value)
                with self.assertRaisesRegex(ProjectValidationError, "must be an integer"):
                    settings.validate()

    def test_integer_analysis_settings_reject_overflowing_values(self) -> None:
        for field_name in (
            "analysis_rate",
            "window_ms",
            "smoothing_windows",
            "waveform_points",
        ):
            with self.subTest(field=field_name):
                settings = AnalysisSettings()
                setattr(settings, field_name, 10**10_000)
                with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
                    settings.validate()
                with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
                    AnalysisSettings.from_dict({field_name: 10**10_000})

    def test_integer_analysis_settings_enforce_supported_maxima(self) -> None:
        for field_name, value in (
            ("analysis_rate", 192_001),
            ("window_ms", 10_001),
            ("smoothing_windows", 100_001),
            ("waveform_points", 1_000_001),
        ):
            with self.subTest(field=field_name):
                with self.assertRaisesRegex(ProjectValidationError, "cannot exceed"):
                    AnalysisSettings.from_dict({field_name: value})

    def test_numeric_analysis_settings_reject_coercive_values(self) -> None:
        numeric_fields = (
            "threshold_margin_db",
            "min_gap_seconds",
            "max_gap_seconds",
            "min_track_seconds",
            "active_run_seconds",
            "lead_in_seconds",
            "tail_seconds",
            "auto_boundary_score",
        )
        for field_name in numeric_fields:
            for invalid in (True, "1.0", None):
                with self.subTest(field=field_name, invalid=invalid):
                    settings = AnalysisSettings()
                    setattr(settings, field_name, invalid)
                    with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
                        settings.validate()
                    with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
                        AnalysisSettings.from_dict({field_name: invalid})

        accepted = AnalysisSettings.from_dict({"threshold_margin_db": 6, "min_gap_seconds": 1.25})
        accepted.validate()

    def test_analysis_summary_rejects_coercive_numbers_in_json_and_models(self) -> None:
        valid = {
            "music_start_seconds": 0.0,
            "music_end_seconds": 10.0,
            "noise_floor_db": -50.0,
            "silence_threshold_db": -44.0,
            "active_threshold_db": -32.0,
            "envelope_window_seconds": 0.05,
            "candidates": [],
            "waveform": [0.0, 0.5],
        }
        numeric_fields = (
            "music_start_seconds",
            "music_end_seconds",
            "noise_floor_db",
            "silence_threshold_db",
            "active_threshold_db",
            "envelope_window_seconds",
        )
        for field_name in numeric_fields:
            for invalid in (True, "1.0", None):
                with self.subTest(field=field_name, invalid=invalid):
                    payload = deepcopy(valid)
                    payload[field_name] = invalid
                    with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
                        AnalysisSummary.from_dict(payload)

                    summary = AnalysisSummary.from_dict(valid)
                    setattr(summary, field_name, invalid)
                    with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
                        summary.validate()

        for invalid in (True, "1.0", None):
            with self.subTest(waveform=invalid):
                payload = deepcopy(valid)
                payload["waveform"] = [invalid]
                with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
                    AnalysisSummary.from_dict(payload)
                summary = AnalysisSummary.from_dict(valid)
                summary.waveform = [invalid]
                with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
                    summary.validate()

        accepted = deepcopy(valid)
        accepted["music_end_seconds"] = 10
        accepted["waveform"] = [0, 0.5]
        AnalysisSummary.from_dict(accepted).validate()

    def test_boundary_candidates_reject_coercive_numbers_in_json_and_models(self) -> None:
        valid = {
            "start_seconds": 4.0,
            "end_seconds": 5.0,
            "cut_seconds": 4.5,
            "cut_sample": 4_500,
            "duration_seconds": 1.0,
            "minimum_db": -60.0,
            "mean_db": -55.0,
            "contrast_db": 12.0,
            "score": 0.8,
            "selected": False,
        }
        numeric_fields = (
            "start_seconds",
            "end_seconds",
            "cut_seconds",
            "duration_seconds",
            "minimum_db",
            "mean_db",
            "contrast_db",
            "score",
        )
        for field_name in numeric_fields:
            for invalid in (True, "1.0", None):
                with self.subTest(field=field_name, invalid=invalid):
                    payload = deepcopy(valid)
                    payload[field_name] = invalid
                    with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
                        BoundaryCandidate.from_dict(payload)

                    candidate = BoundaryCandidate.from_dict(valid)
                    setattr(candidate, field_name, invalid)
                    with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
                        candidate.validate()

        for invalid in (True, "4500", None, 4_500.0):
            with self.subTest(cut_sample=invalid):
                payload = deepcopy(valid)
                payload["cut_sample"] = invalid
                with self.assertRaisesRegex(ProjectValidationError, "must be an integer"):
                    BoundaryCandidate.from_dict(payload)
                candidate = BoundaryCandidate.from_dict(valid)
                candidate.cut_sample = invalid
                with self.assertRaisesRegex(ProjectValidationError, "must be an integer"):
                    candidate.validate()

        accepted = deepcopy(valid)
        accepted["start_seconds"] = 4
        accepted["score"] = 1
        BoundaryCandidate.from_dict(accepted).validate()

    def test_non_text_track_and_source_fields_are_rejected(self) -> None:
        source = AudioSource(
            path="side.flac",
            filename="side.flac",
            size_bytes=1,
            modified_ns=1,
            duration_seconds=1.0,
            sample_rate=1_000,
            channels=2,
            codec_name="flac",
        )
        project = Project(
            source=source,
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(0.0, 1.0, -50.0, -44.0, -32.0, 0.05),
            tracks=[Track(1, "One", 0, 1_000, 0.0, 1.0)],
        )
        for mutation, message in (
            (lambda payload: payload["tracks"][0].__setitem__("title", 123), "title"),
            (
                lambda payload: payload["source"].__setitem__("filename", "../side.flac"),
                "filename",
            ),
            (
                lambda payload: payload["tracks"][0].__setitem__("start_sample", 0.5),
                "sample bounds",
            ),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory_value:
                payload = project.to_dict()
                mutation(payload)
                path = Path(directory_value) / "bad.groove.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ProjectValidationError, message):
                    load_project(path)

    def test_source_integer_fields_reject_extremes_and_inconsistent_counts(self) -> None:
        mutations = (
            ("sample_rate", 10**400),
            ("channels", 10**400),
            ("size_bytes", 10**400),
            ("modified_ns", 10**400),
            ("sample_count", 10**400),
            ("bits_per_raw_sample", 10**400),
        )
        for field_name, value in mutations:
            with self.subTest(field=field_name):
                project = make_history_project()
                setattr(project.source, field_name, value)
                with self.assertRaisesRegex(
                    ProjectValidationError, "finite JSON number|supported range"
                ):
                    project.validate()

        project = make_history_project()
        project.source.sample_count = 20_000
        with self.assertRaisesRegex(ProjectValidationError, "disagrees"):
            project.validate()

    def test_boundary_cut_sample_rejects_extreme_integer(self) -> None:
        candidate = BoundaryCandidate(1.0, 2.0, 1.5, 10**400, 1.0, -50.0, -49.0, 5.0, 0.5)
        with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
            candidate.validate()

    def test_load_and_save_refuse_final_symlink_or_reparse_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            target = directory / "target.groove.json"
            link = directory / "link.groove.json"
            save_project(make_history_project(), target)
            original = target.read_bytes()
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"This host cannot create a test symlink: {exc}")
            with self.assertRaisesRegex(ProjectValidationError, "non-reparse"):
                load_project(link)
            with self.assertRaisesRegex(ProjectValidationError, "non-reparse"):
                save_project(make_history_project(), link)
            self.assertEqual(target.read_bytes(), original)

    def test_save_detects_identity_change_before_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            path = Path(directory_value) / "race.groove.json"
            project = make_history_project()
            save_project(project, path)
            original_revision = project.revision
            real_identity = project_io_module._plain_file_identity
            calls = 0

            def racing_identity(target: Path) -> project_io_module._FileIdentity:
                nonlocal calls
                calls += 1
                if calls == 2:
                    target.write_bytes(b"external replacement")
                return real_identity(target)

            with (
                patch.object(
                    project_io_module,
                    "_plain_file_identity",
                    side_effect=racing_identity,
                ),
                patch.object(project_io_module.os, "replace") as replace_mock,
            ):
                with self.assertRaisesRegex(ProjectValidationError, "identity changed"):
                    save_project(project, path)
                replace_mock.assert_not_called()
            self.assertEqual(path.read_bytes(), b"external replacement")
            self.assertEqual(project.revision, original_revision)


class PersistedEditStateTests(unittest.TestCase):
    def test_state_hash_is_deterministic_and_covers_tracks_and_metadata(self) -> None:
        project = make_history_project()
        first = project_state_sha256(project.tracks, {"artist": "Artist", "album": "Album"})
        reordered = project_state_sha256(project.tracks, {"album": "Album", "artist": "Artist"})
        self.assertEqual(first, reordered)
        self.assertEqual(first, project.state_sha256)

        changed_tracks = ProjectState.capture(project.tracks, project.metadata).tracks
        changed_tracks[0].title = "Changed"
        self.assertNotEqual(first, project_state_sha256(changed_tracks, project.metadata))
        self.assertNotEqual(
            first,
            project_state_sha256(project.tracks, {**project.metadata, "year": "2026"}),
        )

    def test_history_and_checkpoint_round_trip_restore_exact_state(self) -> None:
        project = make_history_project()
        original = project.capture_state()
        project.set_checkpoint("Analyzer accepted", created_at="2026-07-11T12:00:00+00:00")

        before = project.capture_state()
        project.tracks[0].title = "Edited title"
        project.metadata["genre"] = "Metal"
        project.append_history(
            action="edit_track",
            summary="Edited track one and album genre",
            before=before,
            timestamp="2026-07-11T12:01:00+00:00",
        )

        with tempfile.TemporaryDirectory() as directory_value:
            path = Path(directory_value) / "history.groove.json"
            save_project(project, path)
            loaded = load_project(path)

        self.assertEqual(loaded.edit_history[0].before.sha256, original.sha256)
        self.assertEqual(loaded.edit_history[0].after.sha256, loaded.state_sha256)
        self.assertEqual(loaded.checkpoints[0].state.sha256, original.sha256)
        self.assertEqual(loaded.analyzer_baseline.state.sha256, original.sha256)

        before_restore = loaded.capture_state()
        restored = loaded.checkpoint_state("analyzer ACCEPTED")
        loaded.apply_state(restored)
        loaded.append_history(
            action="restore_checkpoint",
            summary="Restored analyzer checkpoint",
            before=before_restore,
            after=restored,
            timestamp="2026-07-11T12:02:00+00:00",
        )
        loaded.validate()
        self.assertEqual(loaded.capture_state().sha256, original.sha256)
        self.assertEqual(loaded.tracks[0].title, "One")
        self.assertNotIn("genre", loaded.metadata)

    def test_legacy_schemas_require_explicit_migration(self) -> None:
        project = make_history_project()
        payload = project.to_dict()
        for schema_version in (1, 2, 3):
            with self.subTest(schema=schema_version):
                legacy = deepcopy(payload)
                legacy["schema_version"] = schema_version
                legacy.pop("analyzer_baseline", None)
                legacy.pop("edit_history", None)
                legacy.pop("checkpoints", None)
                with self.assertRaisesRegex(ProjectValidationError, "project migrate PROJECT"):
                    Project.from_dict(legacy)

    def test_analyze_audio_explicitly_captures_independent_baseline(self) -> None:
        from groove_serpent.analysis import analyze_audio

        source = AudioSource(
            path="side.flac",
            filename="side.flac",
            size_bytes=100,
            modified_ns=1,
            duration_seconds=10.0,
            sample_rate=44_100,
            channels=2,
            codec_name="flac",
            bits_per_raw_sample=16,
            sample_count=441_000,
            sha256="b" * 64,
        )
        with tempfile.TemporaryDirectory() as directory_value:
            source_path = Path(directory_value) / "side.flac"
            source_path.write_bytes(b"x" * 100)
            with (
                patch("groove_serpent.analysis.probe_audio", return_value=source),
                patch(
                    "groove_serpent.analysis.decode_rms_envelope",
                    return_value=(np.full(20, -180.0), 0.5),
                ),
            ):
                project = analyze_audio(
                    source_path,
                    stored_source_path="side.flac",
                    settings=AnalysisSettings(min_track_seconds=0.1),
                    metadata={"album": "Test"},
                )
        self.assertIsInstance(project.analyzer_baseline, AnalyzerBaseline)
        self.assertEqual(project.analyzer_baseline.state_sha256, project.state_sha256)
        project.tracks[0].title = "User edit"
        self.assertNotEqual(project.analyzer_baseline.tracks[0].title, "User edit")

    def test_analyzer_baseline_is_validated_without_changing_current_tracks(self) -> None:
        project = make_history_project()
        original_current = project.capture_state().sha256
        project.analyzer_baseline.tracks[0].end_sample = 4_000
        with self.assertRaisesRegex(ProjectValidationError, "Analyzer baseline"):
            project.validate()
        self.assertEqual(project.capture_state().sha256, original_current)
        self.assertEqual(project.tracks[0].end_sample, 5_000)

    def test_history_is_bounded_and_keeps_a_consecutive_chain(self) -> None:
        project = make_history_project()
        for index in range(MAX_EDIT_HISTORY + 1):
            before = project.capture_state()
            project.metadata["counter"] = str(index)
            project.append_history(
                action="edit_metadata",
                summary=f"Set counter {index}",
                before=before,
                timestamp=f"2026-07-11T12:{index // 60:02d}:{index % 60:02d}+00:00",
            )
        self.assertEqual(len(project.edit_history), MAX_EDIT_HISTORY)
        self.assertEqual(project.edit_history[0].sequence, 2)
        self.assertEqual(project.edit_history[-1].sequence, MAX_EDIT_HISTORY + 1)
        project.validate()

    def test_checkpoint_limit_and_case_insensitive_replacement(self) -> None:
        project = make_history_project()
        for index in range(MAX_CHECKPOINTS):
            project.set_checkpoint(f"Point {index}")
        self.assertEqual(len(project.checkpoints), MAX_CHECKPOINTS)
        replacement = project.set_checkpoint("POINT 0")
        self.assertEqual(len(project.checkpoints), MAX_CHECKPOINTS)
        self.assertEqual(replacement.name, "POINT 0")
        with self.assertRaisesRegex(ProjectValidationError, "cannot contain more"):
            project.set_checkpoint("One too many")

    def test_malformed_nested_types_actions_hashes_and_timestamps_are_rejected(self) -> None:
        project = make_history_project()
        before = project.capture_state()
        project.tracks[0].title = "Edited"
        project.append_history(action="edit_track", summary="Edited title", before=before)
        project.set_checkpoint("Edited")
        payload = project.to_dict()

        mutations = (
            (
                lambda data: data["edit_history"][0].__setitem__("sequence", True),
                "sequence",
            ),
            (
                lambda data: data["edit_history"][0].__setitem__("action", "execute_code"),
                "action",
            ),
            (
                lambda data: data["edit_history"][0].__setitem__("summary", "x" * 513),
                "summary",
            ),
            (
                lambda data: data["edit_history"][0].__setitem__(
                    "timestamp", "2026-07-11T12:00:00"
                ),
                "timezone",
            ),
            (
                lambda data: data["edit_history"][0]["before"]["tracks"][0].__setitem__(
                    "start_sample", "0"
                ),
                "sample bounds",
            ),
            (
                lambda data: data["checkpoints"][0]["state"]["metadata"].__setitem__("year", 2026),
                "text",
            ),
            (
                lambda data: data["analyzer_baseline"].__setitem__("state_sha256", "0" * 64),
                "hash does not match",
            ),
            (
                lambda data: data["checkpoints"][0].__setitem__("name", True),
                "name",
            ),
        )
        for mutation, message in mutations:
            with self.subTest(message=message):
                malformed = deepcopy(payload)
                mutation(malformed)
                with self.assertRaisesRegex(ProjectValidationError, message):
                    Project.from_dict(malformed)

    def test_counts_and_hash_chain_are_rejected_when_malformed(self) -> None:
        project = make_history_project()
        before = project.capture_state()
        project.metadata["one"] = "1"
        project.append_history(action="edit_metadata", summary="First", before=before)
        before = project.capture_state()
        project.metadata["two"] = "2"
        project.append_history(action="edit_metadata", summary="Second", before=before)
        project.set_checkpoint("Current")
        payload = project.to_dict()

        too_much_history = deepcopy(payload)
        too_much_history["edit_history"] = [
            deepcopy(payload["edit_history"][0]) for _ in range(MAX_EDIT_HISTORY + 1)
        ]
        with self.assertRaisesRegex(ProjectValidationError, "cannot exceed"):
            Project.from_dict(too_much_history)

        too_many_checkpoints = deepcopy(payload)
        too_many_checkpoints["checkpoints"] = [
            deepcopy(payload["checkpoints"][0]) for _ in range(MAX_CHECKPOINTS + 1)
        ]
        with self.assertRaisesRegex(ProjectValidationError, "more than"):
            Project.from_dict(too_many_checkpoints)

        broken_chain = deepcopy(payload)
        broken_chain["edit_history"][1]["before"] = deepcopy(
            broken_chain["edit_history"][0]["before"]
        )
        broken_chain["edit_history"][1]["before_sha256"] = broken_chain["edit_history"][0][
            "before_sha256"
        ]
        with self.assertRaisesRegex(ProjectValidationError, "unbroken hash chain"):
            Project.from_dict(broken_chain)

        detached_tail = deepcopy(payload)
        detached_tail["tracks"][0]["title"] = "Unrecorded edit"
        with self.assertRaisesRegex(ProjectValidationError, "latest edit-history"):
            Project.from_dict(detached_tail)

    def test_history_action_allowlist_is_explicit(self) -> None:
        self.assertIn("move_marker", EDIT_ACTION_KINDS)
        self.assertIn("restore_checkpoint", EDIT_ACTION_KINDS)
        self.assertNotIn("arbitrary", EDIT_ACTION_KINDS)

        project = make_history_project()
        state = project.capture_state()
        entry = EditHistoryEntry.create(
            sequence=1,
            action="arbitrary",
            summary="No",
            before=state,
            after=state,
            source_sha256=project.source.sha256,
        )
        project.edit_history = [entry]
        with self.assertRaisesRegex(ProjectValidationError, "not supported"):
            project.validate()


if __name__ == "__main__":
    unittest.main()
