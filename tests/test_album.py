from __future__ import annotations

import hashlib
import io
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from groove_serpent.album import (
    ALBUM_CHAPTERS_NAME,
    ALBUM_CHAPTERS_SCHEMA,
    ALBUM_EXPORT_SCHEMA,
    ALBUM_MANIFEST_NAME,
    AlbumArtwork,
    AlbumExportReport,
    AlbumProject,
    AlbumSide,
    AlbumSpeed,
    SpeedState,
    _cue_quote,
    export_album,
    inspect_album_project,
    load_album_project,
    parse_album_side_spec,
    repin_album_sides,
    save_album_project,
    suggest_album_output_directory,
)
from groove_serpent.cli import main
from groove_serpent.errors import (
    ExportError,
    GrooveSerpentError,
    ProjectValidationError,
)
from groove_serpent.exporter import (
    ExportReport,
    ExportedFile,
    _expected_track_sample_count,
    sanitize_filename,
)
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.media import probe_audio
from groove_serpent.project_io import load_project, save_project
from groove_serpent.publication import stage_verified_copy as real_stage_verified_copy


class AlbumTests(unittest.TestCase):
    def _write_project(
        self,
        root: Path,
        stem: str,
        titles: list[str],
        *,
        sample_rate: int = 1_000,
        metadata: dict[str, str] | None = None,
    ) -> Path:
        source_path = root / f"{stem}.flac"
        source_payload = (f"immutable-{stem}".encode("utf-8")) * 5
        source_path.write_bytes(source_payload)
        source_stat = source_path.stat()
        track_samples = 1_000
        total_samples = track_samples * len(titles)
        tracks = [
            Track(
                number=index,
                title=title,
                start_sample=(index - 1) * track_samples,
                end_sample=index * track_samples,
                start_seconds=(index - 1) * track_samples / sample_rate,
                end_seconds=index * track_samples / sample_rate,
                artist="Side artist",
                album="Side album",
            )
            for index, title in enumerate(titles, start=1)
        ]
        project = Project(
            source=AudioSource(
                path=source_path.name,
                filename=source_path.name,
                size_bytes=source_stat.st_size,
                modified_ns=source_stat.st_mtime_ns,
                duration_seconds=total_samples / sample_rate,
                sample_rate=sample_rate,
                channels=2,
                codec_name="flac",
                bits_per_raw_sample=24,
                sample_format="s32",
                sample_count=total_samples,
                sha256=hashlib.sha256(source_payload).hexdigest(),
            ),
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(
                music_start_seconds=0.0,
                music_end_seconds=total_samples / sample_rate,
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=tracks,
            metadata=dict(metadata or {}),
        )
        project_path = root / f"{stem}.groove.json"
        save_project(project, project_path)
        return project_path

    @staticmethod
    def _fake_side_export(
        project: Project,
        project_path: Path,
        output_dir: Path,
        *,
        formats: list[str],
        overwrite: bool,
        flac_compression: int,
        aac_bitrate: str,
        source_speed_factor: float | None,
        progress: object,
    ) -> ExportReport:
        del project_path, overwrite, flac_compression, aac_bitrate, progress
        output_dir.mkdir()
        offset = int(project.metadata["track_number_offset"])
        files: list[ExportedFile] = []
        for track in project.tracks:
            number = offset + track.number
            expected = _expected_track_sample_count(
                track, project.source.sample_rate, source_speed_factor
            )
            for output_format in formats:
                filename = (
                    f"{number:02d} - "
                    f"{sanitize_filename(track.title, f'Track {number:02d}')}.{output_format}"
                )
                path = output_dir / filename
                payload = f"{number}:{output_format}:{track.title}".encode("utf-8")
                path.write_bytes(payload)
                files.append(
                    ExportedFile(
                        track_number=number,
                        format=output_format,
                        path=filename,
                        size_bytes=len(payload),
                        sha256=hashlib.sha256(payload).hexdigest(),
                        expected_sample_count=expected,
                        presentation_sample_count=(
                            expected if output_format == "m4a" else None
                        ),
                    )
                )
        manifest: dict[str, object] = {"files": [item.path for item in files]}
        if source_speed_factor is not None:
            manifest["speed_correction"] = {
                "source_speed_factor": source_speed_factor,
                "effective_source_speed_factor": source_speed_factor,
                "asetrate_hz": round(project.source.sample_rate / source_speed_factor),
            }
        manifest_path = output_dir / "groove-serpent-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return ExportReport(str(output_dir), files, str(manifest_path))

    @staticmethod
    def _fake_continuous_side(**kwargs: object) -> dict[str, object]:
        project = kwargs["project"]
        side = kwargs["side"]
        destination = kwargs["destination"]
        assert isinstance(project, Project)
        assert isinstance(side, AlbumSide)
        assert isinstance(destination, Path)
        correction = (
            None
            if math.isclose(side.effective_speed_factor, 1.0, abs_tol=1e-12)
            else side.effective_speed_factor
        )
        expected = sum(
            _expected_track_sample_count(track, project.source.sample_rate, correction)
            for track in project.tracks
        )
        payload = f"continuous-side-{side.label}".encode("utf-8")
        destination.write_bytes(payload)
        return {
            "path": destination.as_posix(),
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "expected_sample_count": expected,
            "presentation_sample_count": expected,
        }

    def test_schema_rejects_coercive_numbers_unknown_fields_and_bool(self) -> None:
        valid = {
            "schema": "groove-serpent.speed-state/1",
            "capture_rpm": 33.333333,
            "intended_rpm": 33.333333,
            "fine_factor": 1.0,
        }
        for key, value in (
            ("capture_rpm", "33.333"),
            ("fine_factor", True),
            ("capture_rpm", 10**400),
        ):
            payload = dict(valid)
            payload[key] = value
            with self.subTest(key=key), self.assertRaises(ProjectValidationError):
                SpeedState.from_dict(payload)

        side = AlbumSide("A", 1, "side-a.groove.json")
        payload = asdict(side)
        payload["surprise"] = 1
        with self.assertRaisesRegex(ProjectValidationError, "unsupported field"):
            AlbumSide.from_dict(payload)

    def test_duplicate_side_label_and_project_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            ProjectValidationError, "Duplicate album side label"
        ):
            AlbumProject(
                metadata={},
                sides=[
                    AlbumSide("A", 1, "side-a.groove.json"),
                    AlbumSide("a", 2, "side-b.groove.json"),
                ],
            ).validate()

        with self.assertRaisesRegex(
            ProjectValidationError, "Duplicate album side label"
        ):
            AlbumProject(
                metadata={},
                sides=[
                    AlbumSide("Caf\u00e9", 1, "side-a.groove.json"),
                    AlbumSide("Cafe\u0301", 2, "side-b.groove.json"),
                ],
            ).validate()
        with self.assertRaisesRegex(
            ProjectValidationError, "Duplicate album side project"
        ):
            AlbumProject(
                metadata={},
                sides=[
                    AlbumSide("A", 1, "Caf\u00e9.groove.json"),
                    AlbumSide("B", 2, "Cafe\u0301.groove.json"),
                ],
            ).validate()
        with self.assertRaisesRegex(
            ProjectValidationError, "Duplicate album side project"
        ):
            AlbumProject(
                metadata={},
                sides=[
                    AlbumSide("A", 1, "SIDE.groove.json"),
                    AlbumSide("B", 2, "side.groove.json"),
                ],
            ).validate()

    def test_invalid_combined_speed_factor_is_rejected(self) -> None:
        state = SpeedState(
            capture_rpm=100.0,
            intended_rpm=10.0,
            fine_factor=1.0,
        )
        with self.assertRaisesRegex(ProjectValidationError, "combined"):
            state.validate()

    def test_save_load_round_trip_and_safe_unique_suggestion(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            self._write_project(root, "side-a", ["One"])
            path = root / "album.groove-album.json"
            album = AlbumProject(
                metadata={"artist": "Artist", "album": "Record"},
                sides=[AlbumSide("A", 1, "side-a.groove.json")],
            )
            save_album_project(album, path)
            loaded = load_album_project(path)
            self.assertEqual(loaded.schema, "groove-serpent.album/2")
            self.assertEqual(loaded.sides[0].effective_speed_factor, 1.0)
            self.assertIsNotNone(loaded.sides[0].pin)
            first = suggest_album_output_directory(loaded, path)
            first.mkdir(parents=True)
            second = suggest_album_output_directory(loaded, path)
            self.assertEqual(second.name, "Artist - Record (2)")
            with self.assertRaisesRegex(ProjectValidationError, "already exists"):
                save_album_project(album, path)

    def test_album_suggestion_detects_normalization_equivalent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            path = root / "album.groove-album.json"
            album = AlbumProject(
                metadata={"artist": "Cafe\u0301"},
                sides=[AlbumSide("A", 1, "side-a.groove.json")],
            )
            exports = root / "album-exports"
            exports.mkdir()
            (exports / "Cafe\u0301").mkdir()

            suggestion = suggest_album_output_directory(album, path)

            self.assertEqual(suggestion.name, "Caf\u00e9 (2)")

    def test_album_suggestion_reuses_case_equivalent_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            path = root / "album.groove-album.json"
            album = AlbumProject(
                metadata={"artist": "Artist"},
                sides=[AlbumSide("A", 1, "side-a.groove.json")],
            )
            existing_parent = root / "ALBUM-EXPORTS"
            existing_parent.mkdir()

            suggestion = suggest_album_output_directory(album, path)

            self.assertEqual(suggestion.parent, existing_parent.resolve())

    def test_album_suggestion_reuses_normalization_equivalent_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            existing_project_parent = root / "Cafe\u0301"
            existing_project_parent.mkdir()
            actual_path = existing_project_parent / "album.groove-album.json"
            actual_path.write_text("placeholder", encoding="utf-8")
            album = AlbumProject(
                metadata={"artist": "Artist"},
                sides=[AlbumSide("A", 1, "side-a.groove.json")],
            )

            suggestion = suggest_album_output_directory(
                album,
                root / "Caf\u00e9" / "album.groove-album.json",
            )

            self.assertEqual(
                suggestion.parent.parent,
                existing_project_parent.resolve(),
            )

    def test_album_suggestion_rejects_ambiguous_unicode_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            first = root / "Cafe\u0301"
            second = root / "Caf\u00e9"
            first.mkdir()
            try:
                second.mkdir()
            except FileExistsError:
                self.skipTest("This filesystem normalizes Unicode directory names.")
            album = AlbumProject(
                metadata={"artist": "Artist"},
                sides=[AlbumSide("A", 1, "side-a.groove.json")],
            )

            with self.assertRaisesRegex(ExportError, "ambiguous"):
                suggest_album_output_directory(
                    album,
                    second / "album.groove-album.json",
                )

    def test_explicit_album_export_reuses_unique_portable_ancestors(self) -> None:
        cases = (
            ("case", "LIBRARY", "library"),
            ("normalization", "Cafe\u0301", "Caf\u00e9"),
        )
        for label, existing_name, requested_name in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as value:
                root = Path(value)
                self._write_project(root, "side-a", ["One"])
                album_path = root / "album.groove-album.json"
                album = AlbumProject(
                    metadata={},
                    sides=[AlbumSide("A", 1, "side-a.groove.json")],
                )
                save_album_project(album, album_path)
                existing_parent = root / existing_name
                existing_parent.mkdir()
                output = root / requested_name / "new-batch"

                with mock.patch(
                    "groove_serpent.album.ensure_free_space",
                    side_effect=GrooveSerpentError("preflight sentinel"),
                ) as preflight:
                    with self.assertRaisesRegex(ExportError, "preflight sentinel"):
                        export_album(
                            album,
                            album_path,
                            output,
                            formats=["flac"],
                        )

                self.assertEqual(preflight.call_args.args[0], existing_parent.resolve())
                self.assertFalse((existing_parent / "new-batch").exists())

    def test_album_export_rejects_portable_collision_behind_equivalent_ancestor(
        self,
    ) -> None:
        cases = (
            ("case", "LIBRARY", "library", "Published", "published"),
            (
                "normalization",
                "Cafe\u0301",
                "Caf\u00e9",
                "Re\u0301cord",
                "R\u00e9cord",
            ),
        )
        for label, parent_name, requested_parent, final_name, requested_final in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as value:
                root = Path(value)
                existing_parent = root / parent_name
                existing_parent.mkdir()
                (existing_parent / final_name).mkdir()
                album = AlbumProject(
                    metadata={},
                    sides=[AlbumSide("A", 1, "side-a.groove.json")],
                )

                with self.assertRaisesRegex(ExportError, "already exists"):
                    export_album(
                        album,
                        root / "album.groove-album.json",
                        root / requested_parent / requested_final,
                        formats=["flac"],
                    )

    def test_explicit_album_export_rejects_ambiguous_unicode_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            first = root / "Cafe\u0301"
            second = root / "Caf\u00e9"
            first.mkdir()
            try:
                second.mkdir()
            except FileExistsError:
                self.skipTest("This filesystem normalizes Unicode directory names.")
            album = AlbumProject(
                metadata={},
                sides=[AlbumSide("A", 1, "side-a.groove.json")],
            )

            with self.assertRaisesRegex(ExportError, "ambiguous"):
                export_album(
                    album,
                    root / "album.groove-album.json",
                    second / "new-batch",
                    formats=["flac"],
                )

    def test_album_destination_rejects_normalization_equivalent_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            (root / "Cafe\u0301").mkdir()
            album = AlbumProject(
                metadata={},
                sides=[AlbumSide("A", 1, "side-a.groove.json")],
            )
            with self.assertRaisesRegex(ExportError, "already exists"):
                export_album(
                    album,
                    root / "album.groove-album.json",
                    root / "Caf\u00e9",
                    formats=["flac"],
                )

    def test_album_storage_preflight_fails_before_copy_or_render_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            self._write_project(root, "side-a", ["One", "Two"])
            album_path = root / "album.groove-album.json"
            album = AlbumProject(
                metadata={},
                sides=[AlbumSide("A", 1, "side-a.groove.json")],
            )
            save_album_project(album, album_path)
            output = root / "publication"

            with (
                mock.patch(
                    "groove_serpent.cache_storage.shutil.disk_usage",
                    return_value=mock.Mock(free=1),
                ),
                mock.patch("groove_serpent.album.stage_verified_copy") as copy,
                mock.patch("groove_serpent.album.export_project") as track_export,
                mock.patch(
                    "groove_serpent.album._write_continuous_side"
                ) as side_render,
            ):
                with self.assertRaisesRegex(
                    ExportError,
                    "Album export requires [0-9]+ bytes plus [0-9]+ bytes of "
                    "reserve.*only 1 bytes are available",
                ):
                    export_album(
                        album,
                        album_path,
                        output,
                        formats=["flac"],
                    )

            copy.assert_not_called()
            track_export.assert_not_called()
            side_render.assert_not_called()
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".groove-serpent-album-*.partial")), [])

    def test_short_side_inherits_reviewed_project_speed_and_pins_all_identities(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            project_path = self._write_project(
                root,
                "side-a",
                ["One"],
                metadata={
                    "speed_capture_rpm": "34.000000000",
                    "speed_intended_rpm": "33.333333333",
                    "speed_fine_factor": "1.001000000",
                },
            )
            album_path = root / "album.json"
            self.assertEqual(
                main(
                    [
                        "album",
                        "create",
                        str(album_path),
                        "--side",
                        f"A|{project_path}",
                    ]
                ),
                0,
            )
            album = load_album_project(album_path)
            side = album.sides[0]
            self.assertEqual(side.speed.mode, "inherit")
            self.assertAlmostEqual(
                side.effective_speed_factor,
                34.0 / 33.333333333 * 1.001,
            )
            self.assertEqual(
                side.speed.state_sha256,
                side.speed.project_speed_state_sha256,
            )
            self.assertIsNotNone(side.pin)
            assert side.pin is not None
            self.assertEqual(side.pin.speed_state_sha256, side.speed.state_sha256)
            self.assertEqual(
                side.pin.project_speed_state_sha256,
                side.speed.project_speed_state_sha256,
            )
            receipt = inspect_album_project(album, album_path)
            self.assertTrue(receipt["ready_for_export"])
            side_receipt = receipt["sides"][0]
            self.assertEqual(side_receipt["drift"], [])
            self.assertEqual(
                side_receipt["pin"]["project_revision"],
                side_receipt["current"]["project_revision"],
            )
            self.assertEqual(
                side_receipt["pin"]["project_sha256"],
                side_receipt["current"]["project_sha256"],
            )
            self.assertEqual(
                side_receipt["pin"]["editable_state_sha256"],
                side_receipt["current"]["editable_state_sha256"],
            )
            self.assertEqual(
                side_receipt["pin"]["source_sha256"],
                side_receipt["current"]["source_sha256"],
            )
            self.assertEqual(
                side_receipt["pin"]["project_speed_state_sha256"],
                side_receipt["current"]["project_speed_state_sha256"],
            )

    def test_explicit_speed_override_is_visible_and_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            project_path = self._write_project(
                root,
                "side-a",
                ["One"],
                metadata={
                    "speed_capture_rpm": "34.000000000",
                    "speed_intended_rpm": "33.333333333",
                    "speed_fine_factor": "1.000000000",
                },
            )
            album_path = root / "album.json"
            self.assertEqual(
                main(
                    [
                        "album",
                        "create",
                        str(album_path),
                        "--side",
                        f"A|{project_path}|33.333333333|33.333333333|1.0",
                    ]
                ),
                0,
            )
            album = load_album_project(album_path)
            side = album.sides[0]
            self.assertEqual(side.speed.mode, "override")
            self.assertNotEqual(
                side.speed.state_sha256,
                side.speed.project_speed_state_sha256,
            )
            assert side.pin is not None
            self.assertEqual(side.pin.speed_state_sha256, side.speed.state_sha256)
            receipt = inspect_album_project(album, album_path)["sides"][0]
            self.assertTrue(receipt["speed_override"])
            self.assertTrue(receipt["speed_override_differs_from_project"])
            self.assertTrue(receipt["ready_for_export"])

    def test_project_drift_blocks_export_until_explicit_repin(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            project_path = self._write_project(
                root,
                "side-a",
                ["One"],
                metadata={
                    "speed_capture_rpm": "34.000000000",
                    "speed_intended_rpm": "33.333333333",
                    "speed_fine_factor": "1.000000000",
                },
            )
            album_path = root / "album.json"
            self.assertEqual(
                main(
                    ["album", "create", str(album_path), "--side", f"A|{project_path}"]
                ),
                0,
            )
            project = load_project(project_path)
            before = project.capture_state()
            project.metadata["speed_fine_factor"] = "1.010000000"
            project.append_history(
                action="edit_metadata",
                summary="Changed reviewed speed",
                before=before,
            )
            save_project(project, project_path)

            album = load_album_project(album_path)
            receipt = inspect_album_project(album, album_path)
            self.assertFalse(receipt["ready_for_export"])
            drift = receipt["sides"][0]["drift"]
            self.assertIn("project revision changed", drift)
            self.assertIn("editable project state changed", drift)
            self.assertIn("reviewed project speed state changed", drift)
            with self.assertRaisesRegex(ExportError, "album repin"):
                export_album(album, album_path, root / "blocked", formats=["flac"])
            self.assertFalse((root / "blocked").exists())

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "album",
                            "repin",
                            str(album_path),
                            "--side",
                            "A",
                        ]
                    ),
                    0,
                )
            self.assertIn("Repinned 1 side", output.getvalue())
            repinned = load_album_project(album_path)
            self.assertTrue(
                inspect_album_project(repinned, album_path)["ready_for_export"]
            )
            self.assertAlmostEqual(
                repinned.sides[0].fine_factor,
                1.01,
            )

    def test_changed_source_bytes_are_reported_and_block_repin(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            self._write_project(root, "side", ["One"])
            album_path = root / "album.json"
            album = AlbumProject({}, [AlbumSide("A", 1, "side.groove.json")])
            save_album_project(album, album_path)
            source_path = root / "side.flac"
            source_path.write_bytes(b"X" * source_path.stat().st_size)
            status = inspect_album_project(load_album_project(album_path), album_path)
            self.assertFalse(status["ready_for_export"])
            self.assertIn("source audio changed", status["sides"][0]["drift"])
            with self.assertRaisesRegex(ProjectValidationError, "cannot be pinned"):
                repin_album_sides(load_album_project(album_path), album_path, ["A"])

    def test_legacy_schema_loads_unpinned_and_requires_repin(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            self._write_project(root, "side", ["One"])
            album_path = root / "legacy.json"
            album_path.write_text(
                json.dumps(
                    {
                        "schema": "groove-serpent.album/1",
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "metadata": {},
                        "artwork": None,
                        "sides": [
                            {
                                "label": "A",
                                "order": 1,
                                "project": "side.groove.json",
                                "capture_rpm": 100.0 / 3.0,
                                "intended_rpm": 100.0 / 3.0,
                                "fine_factor": 1.0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            album = load_album_project(album_path)
            self.assertEqual(album.schema, "groove-serpent.album/2")
            self.assertIsNone(album.sides[0].pin)
            self.assertEqual(album.sides[0].speed.mode, "override")
            self.assertFalse(
                inspect_album_project(album, album_path)["ready_for_export"]
            )
            with self.assertRaisesRegex(ExportError, "unpinned"):
                export_album(album, album_path, root / "blocked", formats=["flac"])
            repin_album_sides(album, album_path, ["A"])
            save_album_project(album, album_path, overwrite=True)
            self.assertTrue(
                inspect_album_project(load_album_project(album_path), album_path)[
                    "ready_for_export"
                ]
            )

    def test_78_rpm_transfer_inherits_and_exports_verified_sub_half_factor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            project_path = self._write_project(
                root,
                "shellac",
                ["One"],
                metadata={
                    "speed_capture_rpm": "33.333333333",
                    "speed_intended_rpm": "78.260000000",
                    "speed_fine_factor": "1.000000000",
                },
            )
            album_path = root / "album.json"
            self.assertEqual(
                main(
                    ["album", "create", str(album_path), "--side", f"A|{project_path}"]
                ),
                0,
            )
            album = load_album_project(album_path)
            self.assertAlmostEqual(
                album.sides[0].effective_speed_factor,
                33.333333333 / 78.26,
                places=12,
            )
            seen: list[float | None] = []

            def fake_export(*args: object, **kwargs: object) -> ExportReport:
                seen.append(kwargs["source_speed_factor"])
                return self._fake_side_export(*args, **kwargs)

            with (
                mock.patch(
                    "groove_serpent.album.export_project", side_effect=fake_export
                ),
                mock.patch(
                    "groove_serpent.album._write_continuous_side",
                    side_effect=self._fake_continuous_side,
                ),
            ):
                report = export_album(
                    album,
                    album_path,
                    root / "published",
                    formats=["flac"],
                )
            self.assertEqual(len(seen), 1)
            self.assertAlmostEqual(seen[0] or 0.0, 33.333333333 / 78.26, places=12)
            manifest = json.loads(
                Path(report.manifest_path).read_text(encoding="utf-8")
            )
            self.assertLess(
                manifest["sides"][0]["speed"]["requested_effective_speed_factor"],
                0.5,
            )

    def test_partial_project_speed_metadata_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            project_path = self._write_project(
                root,
                "side",
                ["One"],
                metadata={"speed_capture_rpm": "34.0"},
            )
            with self.assertRaisesRegex(ProjectValidationError, "incomplete"):
                parse_album_side_spec(f"A|{project_path}", 1, root / "album.json")

    def test_album_over_99_tracks_refuses_portability_ambiguous_cue(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            self._write_project(
                root,
                "long-side",
                [f"Track {number}" for number in range(1, 101)],
            )
            album_path = root / "album.json"
            album = AlbumProject({}, [AlbumSide("A", 1, "long-side.groove.json")])
            save_album_project(album, album_path)
            with self.assertRaisesRegex(ExportError, "more than 99 tracks"):
                export_album(album, album_path, root / "blocked", formats=["flac"])
            self.assertFalse((root / "blocked").exists())

    def test_album_export_continuous_numbering_receipts_and_cue(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            self._write_project(root, "side-a", ['One "quoted"', "Two"])
            self._write_project(root, "side-b", ["Three"])
            album_path = root / "record.groove-album.json"
            album = AlbumProject(
                metadata={
                    "artist": 'Artist "Name"\nTRACK 99 AUDIO',
                    "album": 'Record "Title"',
                },
                sides=[
                    AlbumSide("A", 1, "side-a.groove.json"),
                    AlbumSide(
                        "B",
                        2,
                        "side-b.groove.json",
                        speed=AlbumSpeed.create(
                            "override",
                            SpeedState(
                                capture_rpm=34.0,
                                intended_rpm=100.0 / 3.0,
                                fine_factor=1.001,
                            ),
                            None,
                        ),
                    ),
                ],
            )
            save_album_project(album, album_path)
            output = root / "published"
            seen_offsets: list[int] = []
            seen_factors: list[float | None] = []

            def fake_export(*args: object, **kwargs: object) -> ExportReport:
                project = args[0]
                assert isinstance(project, Project)
                seen_offsets.append(int(project.metadata["track_number_offset"]))
                seen_factors.append(kwargs["source_speed_factor"])
                return self._fake_side_export(*args, **kwargs)

            with (
                mock.patch(
                    "groove_serpent.album.export_project", side_effect=fake_export
                ),
                mock.patch(
                    "groove_serpent.album._write_continuous_side",
                    side_effect=self._fake_continuous_side,
                ),
            ):
                report = export_album(
                    album,
                    album_path,
                    output,
                    formats=["flac", "m4a"],
                )

            self.assertEqual(seen_offsets, [0, 2])
            self.assertIsNone(seen_factors[0])
            self.assertGreater(seen_factors[1] or 0.0, 1.0)
            manifest = json.loads(
                Path(report.manifest_path).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["schema"], ALBUM_EXPORT_SCHEMA)
            self.assertEqual(manifest["total_tracks"], 3)
            self.assertEqual(manifest["sides"][0]["track_number_start"], 1)
            self.assertEqual(manifest["sides"][1]["track_number_start"], 3)
            self.assertEqual(
                manifest["sides"][1]["continuous_file"]["expected_sample_count"],
                manifest["sides"][1]["expected_output_sample_count"],
            )
            track_paths = [
                item["path"] for item in manifest["files"] if item["role"] == "track"
            ]
            self.assertTrue(
                any(path.startswith("tracks/01 - ") for path in track_paths)
            )
            self.assertTrue(
                any(path.startswith("tracks/02 - ") for path in track_paths)
            )
            self.assertTrue(
                any(path.startswith("tracks/03 - ") for path in track_paths)
            )
            cue = Path(report.cue_path).read_text(encoding="utf-8")
            self.assertIn("TRACK 03 AUDIO", cue)
            self.assertEqual(cue.count('FILE "sides/'), 2)
            self.assertIn("75 fps approximate", cue)
            self.assertNotIn("\nTRACK 99 AUDIO\n", cue)
            self.assertIn("Artist ''Name'' TRACK 99 AUDIO", cue)
            chapters = json.loads(
                Path(report.chapters_path).read_text(encoding="utf-8")
            )
            self.assertEqual(chapters["schema"], ALBUM_CHAPTERS_SCHEMA)
            self.assertEqual(chapters["precision"], "exact integer sample positions")
            self.assertEqual(
                [
                    (track["output_start_sample"], track["output_end_sample"])
                    for track in chapters["sides"][0]["tracks"]
                ],
                [(0, 1_000), (1_000, 2_000)],
            )
            self.assertEqual(manifest["chapters"]["path"], ALBUM_CHAPTERS_NAME)
            self.assertEqual(manifest["cue"]["timebase_frames_per_second"], 75)
            self.assertEqual(
                hashlib.sha256((root / "side-a.flac").read_bytes()).hexdigest(),
                manifest["sides"][0]["source_sha256"],
            )

    def test_album_artwork_and_side_manifest_use_verified_copies(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            self._write_project(root, "side-a", ["One"])
            artwork_bytes = b"\x89PNG\r\n\x1a\napproved-cover"
            artwork_path = root / "cover.png"
            artwork_path.write_bytes(artwork_bytes)
            album_path = root / "album.json"
            album = AlbumProject(
                {"artist": "Artist", "album": "Album"},
                [AlbumSide("A", 1, "side-a.groove.json")],
                artwork=AlbumArtwork(
                    artwork_path.name,
                    hashlib.sha256(artwork_bytes).hexdigest(),
                ),
            )
            save_album_project(album, album_path)
            labels: list[str] = []

            def observed_copy(*args: object, **kwargs: object):
                labels.append(str(kwargs["label"]))
                return real_stage_verified_copy(*args, **kwargs)

            output = root / "published"
            with (
                mock.patch(
                    "groove_serpent.album.export_project",
                    side_effect=self._fake_side_export,
                ),
                mock.patch(
                    "groove_serpent.album._write_continuous_side",
                    side_effect=self._fake_continuous_side,
                ),
                mock.patch(
                    "groove_serpent.album.stage_verified_copy",
                    side_effect=observed_copy,
                ),
            ):
                report = export_album(album, album_path, output, formats=["flac"])

            self.assertIn("Published album artwork", labels)
            self.assertIn("Side A artwork", labels)
            self.assertIn("Published Side A manifest", labels)
            manifest = json.loads(
                Path(report.manifest_path).read_text(encoding="utf-8")
            )
            published_artwork = output / manifest["artwork"]["path"]
            inventory_artwork = next(
                item for item in manifest["files"] if item["role"] == "artwork"
            )
            actual_sha256 = hashlib.sha256(published_artwork.read_bytes()).hexdigest()
            self.assertEqual(actual_sha256, album.artwork.sha256)
            self.assertEqual(actual_sha256, manifest["artwork"]["sha256"])
            self.assertEqual(actual_sha256, inventory_artwork["sha256"])

    def test_album_export_refuses_corrupted_verified_publication_copies(self) -> None:
        for corrupt_label in (
            "Published album artwork",
            "Published Side A manifest",
        ):
            with self.subTest(label=corrupt_label), tempfile.TemporaryDirectory() as value:
                root = Path(value)
                self._write_project(root, "side-a", ["One"])
                artwork_bytes = b"\x89PNG\r\n\x1a\napproved-cover"
                artwork_path = root / "cover.png"
                artwork_path.write_bytes(artwork_bytes)
                album_path = root / "album.json"
                album = AlbumProject(
                    {"artist": "Artist", "album": "Album"},
                    [AlbumSide("A", 1, "side-a.groove.json")],
                    artwork=AlbumArtwork(
                        artwork_path.name,
                        hashlib.sha256(artwork_bytes).hexdigest(),
                    ),
                )
                save_album_project(album, album_path)

                def corrupt_copy(*args: object, **kwargs: object):
                    receipt = real_stage_verified_copy(*args, **kwargs)
                    if kwargs["label"] == corrupt_label:
                        destination = args[1]
                        assert isinstance(destination, Path)
                        destination.write_bytes(b"corrupted-after-verified-copy")
                    return receipt

                output = root / "published"
                with (
                    mock.patch(
                        "groove_serpent.album.export_project",
                        side_effect=self._fake_side_export,
                    ),
                    mock.patch(
                        "groove_serpent.album._write_continuous_side",
                        side_effect=self._fake_continuous_side,
                    ),
                    mock.patch(
                        "groove_serpent.album.stage_verified_copy",
                        side_effect=corrupt_copy,
                    ),
                    self.assertRaisesRegex(ExportError, "failed its verified receipt"),
                ):
                    export_album(album, album_path, output, formats=["flac"])

                self.assertFalse(output.exists())
                self.assertEqual(
                    list(root.glob(".groove-serpent-album-*.partial")), []
                )

    def test_existing_output_is_rejected_without_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            self._write_project(root, "side", ["One"])
            album_path = root / "album.json"
            album = AlbumProject({}, [AlbumSide("A", 1, "side.groove.json")])
            save_album_project(album, album_path)
            output = root / "exists"
            output.mkdir()
            marker = output / "mine.txt"
            marker.write_text("untouched", encoding="utf-8")
            with self.assertRaisesRegex(ExportError, "already exists"):
                export_album(album, album_path, output, formats=["flac"])
            self.assertEqual(marker.read_text(encoding="utf-8"), "untouched")

    def test_failed_side_export_cleans_outer_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            self._write_project(root, "side-a", ["One"])
            self._write_project(root, "side-b", ["Two"])
            album_path = root / "album.json"
            album = AlbumProject(
                {},
                [
                    AlbumSide("A", 1, "side-a.groove.json"),
                    AlbumSide("B", 2, "side-b.groove.json"),
                ],
            )
            save_album_project(album, album_path)
            output = root / "failed"
            calls = 0

            def failing_export(*args: object, **kwargs: object) -> ExportReport:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise ExportError("synthetic side failure")
                return self._fake_side_export(*args, **kwargs)

            with (
                mock.patch(
                    "groove_serpent.album.export_project", side_effect=failing_export
                ),
                mock.patch(
                    "groove_serpent.album._write_continuous_side",
                    side_effect=self._fake_continuous_side,
                ),
                self.assertRaisesRegex(ExportError, "synthetic side failure"),
            ):
                export_album(album, album_path, output, formats=["flac"])

            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".groove-serpent-album-*.partial")), [])

    def test_cue_quote_neutralizes_directives_and_literal_quotes(self) -> None:
        rendered = _cue_quote('Line one"\nFILE "evil.flac" MP3')
        self.assertNotIn("\n", rendered)
        self.assertEqual(rendered.count('"'), 2)
        self.assertIn("Line one'' FILE ''evil.flac'' MP3", rendered)

    def test_nested_album_cli_create_inspect_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            project_path = self._write_project(root, "side-a", ["One"])
            album_path = root / "album.json"
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "album",
                        "create",
                        str(album_path),
                        "--side",
                        f"A|{project_path}|33.5|33.333333|1.001",
                        "--artist",
                        "Artist",
                        "--album",
                        "Record",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertTrue(album_path.is_file())
            self.assertIn("speed=", output.getvalue())

            inspected = io.StringIO()
            with redirect_stdout(inspected):
                result = main(["album", "inspect", str(album_path), "--json"])
            self.assertEqual(result, 0)
            receipt = json.loads(inspected.getvalue())
            self.assertEqual(receipt["total_tracks"], 1)

            destination = root / "album-export"
            fake_report = AlbumExportReport(
                output_directory=str(destination),
                files=[],
                manifest_path=str(destination / ALBUM_MANIFEST_NAME),
                cue_path=str(destination / "album.cue"),
            )
            exported = io.StringIO()
            with (
                mock.patch(
                    "groove_serpent.album.export_album", return_value=fake_report
                ) as export_call,
                redirect_stdout(exported),
            ):
                result = main(
                    [
                        "album",
                        "export",
                        str(album_path),
                        "--output-dir",
                        str(destination),
                        "--formats",
                        "flac",
                    ]
                )
            self.assertEqual(result, 0)
            export_call.assert_called_once()
            self.assertIn("Manifest:", exported.getvalue())

    def test_cli_create_resolves_relative_inputs_from_album_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            self._write_project(root, "side-a", ["One"])
            artwork_dir = root / "artwork"
            artwork_dir.mkdir()
            artwork = artwork_dir / "cover.png"
            artwork.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test")
            album_path = root / "album.json"

            result = main(
                [
                    "album",
                    "create",
                    str(album_path),
                    "--side",
                    "A|side-a.groove.json",
                    "--artwork",
                    "artwork/cover.png",
                ]
            )

            self.assertEqual(result, 0)
            album = load_album_project(album_path)
            self.assertEqual(album.sides[0].project, "side-a.groove.json")
            self.assertIsNotNone(album.artwork)
            assert album.artwork is not None
            self.assertEqual(album.artwork.path, "artwork/cover.png")

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required"
    )
    def test_real_flac_album_export_has_exact_continuous_side_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            source_path = root / "side.flac"
            subprocess.run(
                [
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=1",
                    "-ac",
                    "2",
                    "-c:a",
                    "flac",
                    "-sample_fmt",
                    "s16",
                    str(source_path),
                ],
                check=True,
            )
            source = probe_audio(source_path, stored_path=source_path.name)
            self.assertEqual(source.sample_count, 48_000)
            project = Project(
                source=source,
                settings=AnalysisSettings(min_track_seconds=0.1),
                analysis=AnalysisSummary(
                    music_start_seconds=0.0,
                    music_end_seconds=1.0,
                    noise_floor_db=-60.0,
                    silence_threshold_db=-54.0,
                    active_threshold_db=-42.0,
                    envelope_window_seconds=0.05,
                ),
                tracks=[
                    Track(1, "First", 0, 24_000, 0.0, 0.5),
                    Track(2, "Second", 24_000, 48_000, 0.5, 1.0),
                ],
            )
            project_path = root / "side.groove.json"
            save_project(project, project_path)
            album_path = root / "album.json"
            album = AlbumProject(
                {"artist": "Test Artist", "album": "Test Album"},
                [AlbumSide("A", 1, project_path.name)],
            )
            save_album_project(album, album_path)
            output = root / "exported"
            report = export_album(album, album_path, output, formats=["flac"])

            manifest = json.loads(
                Path(report.manifest_path).read_text(encoding="utf-8")
            )
            continuous = output / manifest["sides"][0]["continuous_file"]["path"]
            probed = probe_audio(continuous)
            self.assertEqual(probed.sample_count, 48_000)
            self.assertEqual(
                manifest["sides"][0]["expected_output_sample_count"], 48_000
            )
            self.assertEqual(
                [
                    item["expected_output_sample_count"]
                    for item in manifest["sides"][0]["tracks"]
                ],
                [24_000, 24_000],
            )
            self.assertEqual(manifest["schema"], "groove-serpent.album-export/2")
            self.assertTrue(
                manifest["verification"]["continuous_side_flacs_completely_decoded"]
            )
            self.assertTrue(
                manifest["sides"][0]["continuous_file"]["verification"][
                    "archival_pcm_equal"
                ]
            )

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required"
    )
    def test_album_export_refuses_source_replacement_during_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            source_path = root / "side.flac"
            alternate_path = root / "alternate.flac"
            ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
            for destination, frequency in (
                (source_path, 440),
                (alternate_path, 880),
            ):
                subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        f"sine=frequency={frequency}:sample_rate=48000:duration=1",
                        "-ac",
                        "2",
                        "-c:a",
                        "flac",
                        "-sample_fmt",
                        "s16",
                        str(destination),
                    ],
                    check=True,
                )

            source = probe_audio(source_path, stored_path=source_path.name)
            project = Project(
                source=source,
                settings=AnalysisSettings(min_track_seconds=0.1),
                analysis=AnalysisSummary(
                    music_start_seconds=0.0,
                    music_end_seconds=1.0,
                    noise_floor_db=-60.0,
                    silence_threshold_db=-54.0,
                    active_threshold_db=-42.0,
                    envelope_window_seconds=0.05,
                ),
                tracks=[
                    Track(1, "First", 0, 24_000, 0.0, 0.5),
                    Track(2, "Second", 24_000, 48_000, 0.5, 1.0),
                ],
            )
            project_path = root / "side.groove.json"
            save_project(project, project_path)
            album_path = root / "album.json"
            album = AlbumProject(
                {"artist": "Test Artist", "album": "Test Album"},
                [AlbumSide("A", 1, project_path.name)],
            )
            save_album_project(album, album_path)
            output = root / "exported"
            replaced = False

            def replace_live_source(message: str) -> None:
                nonlocal replaced
                if not replaced and message.startswith("Exporting track"):
                    shutil.copyfile(alternate_path, source_path)
                    replaced = True

            with self.assertRaisesRegex(ExportError, "changed during export"):
                export_album(
                    album,
                    album_path,
                    output,
                    formats=["flac"],
                    progress=replace_live_source,
                )

            self.assertTrue(replaced)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".groove-serpent-album-*.partial")), [])


if __name__ == "__main__":
    unittest.main()
