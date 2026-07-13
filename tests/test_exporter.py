from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import groove_serpent.exporter as exporter_module
import groove_serpent.portable_names as portable_names_module
from groove_serpent.errors import ExportError, GrooveSerpentError
from groove_serpent.exporter import (
    _build_command,
    _expected_track_sample_count,
    _speed_corrected_sample,
    _speed_correction_details,
    export_project,
    sanitize_filename,
    suggest_output_directory,
)
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.media import probe_audio
from groove_serpent.project_io import save_project
from groove_serpent.publication import FileReceipt, canonical_json_sha256
from groove_serpent.portable_names import PortablePathError, resolve_portable_path


class ExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        def fake_verification(**values):
            presentation = None
            if values["output_format"] == "m4a":
                presentation = exporter_module._probe_m4a_presentation_sample_count(
                    values["staged_path"], values["source_sample_rate"]
                )
                if presentation != values["expected_sample_count"]:
                    raise ExportError(
                        f"Staged M4A '{values['staged_path'].name}' has {presentation} "
                        f"presentation samples; expected exactly "
                        f"{values['expected_sample_count']}. The incomplete batch was not "
                        "published."
                    )
            archival_hash = (
                "c" * 64
                if values["output_format"] == "flac"
                and values["source_speed_factor"] is None
                else None
            )
            return exporter_module._StagedAudioVerification(
                codec_name="flac" if values["output_format"] == "flac" else "aac",
                sample_rate=values["source_sample_rate"],
                channels=values["source_channels"],
                bits_per_raw_sample=(
                    24
                    if values["source_bits"] is not None and values["source_bits"] > 16
                    else 16
                ),
                exact_sample_count=values["expected_sample_count"],
                presentation_sample_count=presentation,
                decoded_pcm_sha256=(
                    "c" * 64 if values["output_format"] == "flac" else None
                ),
                source_range_pcm_sha256=archival_hash,
            )

        patcher = mock.patch(
            "groove_serpent.exporter._verify_staged_output",
            side_effect=fake_verification,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_file_receipt_tolerates_windows_handle_path_ctime_precision_skew(
        self,
    ) -> None:
        handle_receipt = FileReceipt(
            sha256="a" * 64,
            size_bytes=123,
            modified_ns=456,
            status_changed_ns=789_500_200,
            device=12,
            inode=34,
            mode=0o100666,
            birth_ns=789_000_000,
            file_attributes=32,
        )
        path_receipt = replace(handle_receipt, status_changed_ns=789_000_000)

        self.assertNotEqual(handle_receipt, path_receipt)
        self.assertTrue(handle_receipt.same_file_object(path_receipt))

    def test_file_receipt_accepts_stat_without_optional_platform_fields(self) -> None:
        portable_stat = SimpleNamespace(
            st_size=123,
            st_mtime_ns=2_000_000_000,
            st_ctime_ns=3_000_000_000,
            st_dev=22,
            st_ino=11,
            st_mode=0o100644,
        )

        receipt = FileReceipt.from_stat(portable_stat, "a" * 64)

        self.assertIsNone(receipt.birth_ns)
        self.assertIsNone(receipt.file_attributes)
        self.assertEqual(receipt.size_bytes, 123)

    def _project(self, source_path: Path, *, bits: int | None, rate: int) -> Project:
        stat = source_path.stat()
        source = AudioSource(
            path=source_path.name,
            filename=source_path.name,
            size_bytes=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            duration_seconds=1.0,
            sample_rate=rate,
            channels=2,
            codec_name="flac",
            bits_per_raw_sample=bits,
            sample_format="s32" if bits and bits > 16 else "s16",
            sample_count=rate,
            sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
        )
        return Project(
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
                Track(
                    number=1,
                    title="One",
                    start_sample=0,
                    end_sample=rate,
                    start_seconds=0.0,
                    end_seconds=1.0,
                )
            ],
        )

    def _two_track_project(
        self, source_path: Path, *, bits: int | None = 24, rate: int = 48_000
    ) -> Project:
        project = self._project(source_path, bits=bits, rate=rate)
        split = rate // 2
        project.tracks = [
            Track(1, "One", 0, split, 0.0, split / rate),
            Track(2, "Two", split, rate, split / rate, 1.0),
        ]
        return project

    def test_windows_unsafe_characters_are_replaced(self) -> None:
        self.assertEqual(sanitize_filename("A/B: C?*", "Track"), "A_B_ C__")
        self.assertEqual(sanitize_filename("CON", "Track"), "_CON")

    def test_unicode_is_preserved(self) -> None:
        self.assertEqual(sanitize_filename("Café – 夜の歌", "Track"), "Café – 夜の歌")

    def test_generated_filenames_use_canonical_unicode_normalization(self) -> None:
        self.assertEqual(sanitize_filename("Cafe\u0301", "Track"), "Caf\u00e9")

    def test_generated_filename_budgets_include_prefix_and_extension(self) -> None:
        cases = (
            "\U0001f3b5" * 100,
            "\u591c\u306e\u6b4c" * 100,
        )
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            for title in cases:
                with self.subTest(title=title[:4]):
                    output_path = exporter_module._output_path(
                        directory,
                        Track(1, title, 0, 1, 0.0, 1.0),
                        "flac",
                    )
                    component = output_path.name
                    self.assertLessEqual(len(component.encode("utf-8")), 240)
                    self.assertLessEqual(
                        len(component.encode("utf-16-le")) // 2,
                        240,
                    )
                    self.assertRegex(output_path.stem, r" ~[0-9a-f]{10}$")
                    output_path.write_bytes(b"portable")
                    self.assertEqual(output_path.read_bytes(), b"portable")

    def test_long_canonical_equivalents_generate_the_same_filename(self) -> None:
        prefix = "01 - "
        suffix = ".flac"
        composed = "Caf\u00e9 " * 100
        decomposed = "Cafe\u0301 " * 100

        composed_name = sanitize_filename(
            composed,
            "Track 01",
            prefix=prefix,
            suffix=suffix,
        )
        decomposed_name = sanitize_filename(
            decomposed,
            "Track 01",
            prefix=prefix,
            suffix=suffix,
        )

        self.assertEqual(composed_name, decomposed_name)
        self.assertEqual(
            composed_name,
            portable_names_module.normalize_portable_name(composed_name),
        )

    def test_long_titles_differing_after_truncation_keep_distinct_hashes(self) -> None:
        prefix = "01 - "
        suffix = ".flac"
        shared = "\U0001f3b5" * 100
        first = sanitize_filename(
            f"{shared} first",
            "Track 01",
            prefix=prefix,
            suffix=suffix,
        )
        second = sanitize_filename(
            f"{shared} second",
            "Track 01",
            prefix=prefix,
            suffix=suffix,
        )

        self.assertNotEqual(first, second)
        self.assertNotEqual(
            portable_names_module.portable_name_key(f"{prefix}{first}{suffix}"),
            portable_names_module.portable_name_key(f"{prefix}{second}{suffix}"),
        )

    def test_output_suggestion_is_album_readable_and_collision_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            source = root / "side.flac"
            source.write_bytes(b"source")
            project = self._project(source, bits=24, rate=48_000)
            project.metadata.update(
                {
                    "artist": "The Test Pressings",
                    "album": "Signals from the Workbench",
                    "side": "A",
                }
            )
            project_path = root / "side.groove.json"
            first = suggest_output_directory(project, project_path)
            self.assertEqual(
                first,
                root.resolve()
                / "exports"
                / "The Test Pressings - Signals from the Workbench - Side A",
            )
            first.mkdir(parents=True)
            self.assertEqual(
                suggest_output_directory(project, project_path),
                root.resolve()
                / "exports"
                / "The Test Pressings - Signals from the Workbench - Side A - batch 02",
            )

    def test_output_suggestion_detects_normalization_equivalent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            source = root / "side.flac"
            source.write_bytes(b"source")
            project = self._project(source, bits=24, rate=48_000)
            project.metadata.update({"artist": "Cafe\u0301"})
            exports = root / "exports"
            exports.mkdir()
            (exports / "Cafe\u0301").mkdir()

            suggestion = suggest_output_directory(
                project, root / "side.groove.json"
            )

            self.assertEqual(suggestion.name, "Caf\u00e9 - batch 02")

    def test_output_suggestion_reuses_case_equivalent_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            source = root / "side.flac"
            source.write_bytes(b"source")
            project = self._project(source, bits=24, rate=48_000)
            existing_parent = root / "EXPORTS"
            existing_parent.mkdir()

            suggestion = suggest_output_directory(
                project, root / "side.groove.json"
            )

            self.assertEqual(suggestion.parent, existing_parent.resolve())

    def test_output_suggestion_reuses_normalization_equivalent_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            existing_project_parent = root / "Cafe\u0301"
            existing_project_parent.mkdir()
            source = existing_project_parent / "side.flac"
            source.write_bytes(b"source")
            project = self._project(source, bits=24, rate=48_000)
            actual_project = existing_project_parent / "side.groove.json"
            actual_project.write_text("placeholder", encoding="utf-8")

            suggestion = suggest_output_directory(
                project,
                root / "Caf\u00e9" / "side.groove.json",
            )

            self.assertEqual(
                suggestion.parent.parent,
                existing_project_parent.resolve(),
            )

    def test_output_suggestion_rejects_ambiguous_unicode_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            first = root / "Cafe\u0301"
            second = root / "Caf\u00e9"
            first.mkdir()
            try:
                second.mkdir()
            except FileExistsError:
                self.skipTest("This filesystem normalizes Unicode directory names.")
            source = first / "side.flac"
            source.write_bytes(b"source")
            project = self._project(source, bits=24, rate=48_000)

            with self.assertRaisesRegex(ExportError, "ambiguous"):
                suggest_output_directory(
                    project,
                    second / "side.groove.json",
                )

    def test_explicit_export_reuses_unique_portable_equivalent_ancestors(
        self,
    ) -> None:
        cases = (
            ("case", "LIBRARY", "library"),
            ("normalization", "Cafe\u0301", "Caf\u00e9"),
        )
        for label, existing_name, requested_name in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as value:
                root = Path(value)
                source = root / "side.flac"
                source.write_bytes(b"source")
                project = self._project(source, bits=24, rate=48_000)
                existing_parent = root / existing_name
                existing_parent.mkdir()
                output = root / requested_name / "new-batch"

                with (
                    mock.patch(
                        "groove_serpent.exporter.probe_audio",
                        return_value=project.source,
                    ),
                    mock.patch(
                        "groove_serpent.exporter.tool_version",
                        return_value="test",
                    ),
                    mock.patch(
                        "groove_serpent.exporter.ensure_free_space",
                        side_effect=GrooveSerpentError("preflight sentinel"),
                    ) as preflight,
                ):
                    with self.assertRaisesRegex(ExportError, "preflight sentinel"):
                        export_project(
                            project,
                            root / "side.groove.json",
                            output,
                            formats=["flac"],
                        )

                self.assertEqual(preflight.call_args.args[0], existing_parent.resolve())
                self.assertFalse((existing_parent / "new-batch").exists())

    def test_explicit_export_rejects_collision_behind_equivalent_ancestor(
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
                source = root / "side.flac"
                source.write_bytes(b"source")
                project = self._project(source, bits=24, rate=48_000)

                with self.assertRaisesRegex(ExportError, "already exists"):
                    export_project(
                        project,
                        root / "side.groove.json",
                        root / requested_parent / requested_final,
                        formats=["flac"],
                    )

    def test_explicit_export_rejects_ambiguous_unicode_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            first = root / "Cafe\u0301"
            second = root / "Caf\u00e9"
            first.mkdir()
            try:
                second.mkdir()
            except FileExistsError:
                self.skipTest("This filesystem normalizes Unicode directory names.")
            source = root / "side.flac"
            source.write_bytes(b"source")
            project = self._project(source, bits=24, rate=48_000)

            with self.assertRaisesRegex(ExportError, "ambiguous"):
                export_project(
                    project,
                    root / "side.groove.json",
                    second / "new-batch",
                    formats=["flac"],
                )

    def test_explicit_export_rejects_ambiguous_casefold_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            first = root / "LIBRARY"
            second = root / "library"
            first.mkdir()
            try:
                second.mkdir()
            except FileExistsError:
                self.skipTest("This filesystem is case-insensitive.")
            source = root / "side.flac"
            source.write_bytes(b"source")
            project = self._project(source, bits=24, rate=48_000)

            with self.assertRaisesRegex(ExportError, "ambiguous"):
                export_project(
                    project,
                    root / "side.groove.json",
                    second / "new-batch",
                    formats=["flac"],
                )

    def test_explicit_export_rejects_redirecting_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            outside = root / "outside"
            outside.mkdir()
            link = root / "library"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Directory symlinks are not available on this host.")
            source = root / "side.flac"
            source.write_bytes(b"source")
            project = self._project(source, bits=24, rate=48_000)

            with self.assertRaisesRegex(
                ExportError, "symlink or reparse point"
            ) as raised:
                export_project(
                    project,
                    root / "side.groove.json",
                    link / "new-batch",
                    formats=["flac"],
                )

            self.assertIn(str(link), str(raised.exception))
            self.assertEqual(list(outside.iterdir()), [])

    def test_portable_directory_cache_invalidates_for_new_equivalent_sibling(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            first = root / "Cafe\u0301"
            second = root / "Caf\u00e9"
            first.mkdir()
            target = second / "new-batch"
            portable_names_module._clear_directory_cache()

            initial = resolve_portable_path(target)
            self.assertFalse(initial.entry_exists)
            self.assertEqual(initial.path.parent, first)
            try:
                second.mkdir()
            except FileExistsError:
                self.skipTest("This filesystem normalizes Unicode directory names.")

            with self.assertRaisesRegex(PortablePathError, "ambiguous"):
                resolve_portable_path(target)

    def test_portable_directory_cache_is_safe_under_concurrent_resolution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            existing = root / "Cafe\u0301"
            existing.mkdir()
            target = root / "Caf\u00e9" / "new-batch"
            portable_names_module._clear_directory_cache()

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(
                    executor.map(
                        lambda _index: resolve_portable_path(target),
                        range(64),
                    )
                )

            self.assertTrue(all(not result.entry_exists for result in results))
            self.assertTrue(all(result.path.parent == existing for result in results))

    def test_storage_preflight_fails_before_snapshot_or_encoder_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            source = root / "side.flac"
            source.write_bytes(b"source")
            project = self._project(source, bits=24, rate=48_000)
            output = root / "publication"

            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio",
                    return_value=project.source,
                ),
                mock.patch(
                    "groove_serpent.exporter.tool_version", return_value="test"
                ),
                mock.patch(
                    "groove_serpent.cache_storage.shutil.disk_usage",
                    return_value=SimpleNamespace(free=1),
                ),
                mock.patch("groove_serpent.exporter.stage_verified_copy") as copy,
                mock.patch("groove_serpent.exporter.run_ffmpeg") as encoder,
            ):
                with self.assertRaisesRegex(
                    ExportError,
                    "Track export requires [0-9]+ bytes plus [0-9]+ bytes of "
                    "reserve.*only 1 bytes are available",
                ):
                    export_project(
                        project,
                        root / "side.groove.json",
                        output,
                        formats=["flac"],
                    )

            copy.assert_not_called()
            encoder.assert_not_called()
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".groove-serpent-export-*.partial")), [])

    def test_m4a_uses_source_rate_as_movie_timescale(self) -> None:
        track = Track(1, "One", 7, 48_007, 7 / 48_000, 48_007 / 48_000)
        with mock.patch("groove_serpent.exporter.find_tool", return_value="ffmpeg"):
            command = _build_command(
                source_path=Path("source.flac"),
                output_path=Path("track.m4a"),
                track=track,
                total_tracks=1,
                output_format="m4a",
                source_sample_rate=48_000,
                source_bits=24,
                overwrite=True,
                flac_compression=8,
                aac_bitrate="256k",
            )
        index = command.index("-movie_timescale")
        self.assertEqual(command[index + 1], "48000")
        self.assertEqual(
            command[command.index("-movflags") + 1], "+faststart+use_metadata_tags"
        )
        self.assertTrue(
            command[command.index("-af") + 1].endswith("asettb=expr=1/48000,asetpts=N")
        )

    def test_command_embeds_stable_release_and_catalog_metadata(self) -> None:
        track = Track(
            2,
            "Two",
            48_000,
            96_000,
            1.0,
            2.0,
            side="B",
            musicbrainz_recording_id="05df1765-62c0-4977-8959-bea4465e7e93",
            musicbrainz_track_id="f02df099-2df0-37e3-b388-0eadc5175af3",
        )
        metadata = {
            "musicbrainz_release_id": "62d1c4ef-fc00-37af-8df7-485f6a31fcc4",
            "musicbrainz_release_group_id": "0ef97d52-3f00-31bf-8413-f83ccb362675",
            "musicbrainz_medium_position": "2",
            "barcode": "012345678905",
            "label": "Round Records",
            "catalog_number": "RR 42",
        }
        with mock.patch("groove_serpent.exporter.find_tool", return_value="ffmpeg"):
            command = _build_command(
                source_path=Path("source.flac"),
                output_path=Path("track.flac"),
                track=track,
                total_tracks=9,
                output_format="flac",
                source_sample_rate=48_000,
                source_bits=24,
                overwrite=True,
                flac_compression=8,
                aac_bitrate="256k",
                project_metadata=metadata,
            )

        for expected in (
            "track=2/9",
            "tracktotal=9",
            "grouping=Side B",
            "vinyl_side=B",
            "disc=2",
            "musicbrainz_albumid=62d1c4ef-fc00-37af-8df7-485f6a31fcc4",
            "musicbrainz_releasegroupid=0ef97d52-3f00-31bf-8413-f83ccb362675",
            "musicbrainz_recordingid=05df1765-62c0-4977-8959-bea4465e7e93",
            "musicbrainz_trackid=f02df099-2df0-37e3-b388-0eadc5175af3",
            "barcode=012345678905",
            "publisher=Round Records",
            "catalog_number=RR 42",
        ):
            self.assertIn(expected, command)

    def test_command_applies_explicit_pitch_and_tempo_speed_correction(self) -> None:
        track = Track(1, "Fast capture", 100, 44_200, 100 / 44_100, 44_200 / 44_100)
        with mock.patch("groove_serpent.exporter.find_tool", return_value="ffmpeg"):
            command = _build_command(
                source_path=Path("source.flac"),
                output_path=Path("corrected.flac"),
                track=track,
                total_tracks=1,
                output_format="flac",
                source_sample_rate=44_100,
                source_bits=24,
                overwrite=True,
                flac_compression=8,
                aac_bitrate="256k",
                source_speed_factor=1.039,
            )

        filter_graph = command[command.index("-af") + 1]
        self.assertIn("asetrate=42445", filter_graph)
        self.assertIn("aresample=44100:resampler=soxr:precision=33", filter_graph)
        self.assertIn("atrim=start_sample=104:end_sample=45923", filter_graph)
        self.assertLess(filter_graph.index("asetrate"), filter_graph.index("atrim"))
        self.assertTrue(filter_graph.endswith("asettb=expr=1/44100,asetpts=N"))
        self.assertIn("groove_serpent_source_speed_factor=1.039000000", command)
        self.assertIn(
            "groove_serpent_effective_speed_factor=1.038991636235",
            command,
        )
        self.assertIn("groove_serpent_asetrate_hz=42445", command)
        self.assertIn(
            "groove_serpent_speed_correction=pitch-and-tempo together; integer asetrate + libsoxr",
            command,
        )
        self.assertIn(
            "comment=Split and speed-corrected by Groove Serpent; source factor 1.039000000",
            command,
        )

    def test_expected_corrected_counts_share_one_deterministic_global_grid(
        self,
    ) -> None:
        rng = random.Random(0x5EED)
        for _ in range(250):
            sample_rate = rng.choice((32_000, 44_100, 48_000, 88_200, 96_000))
            source_speed_factor = rng.uniform(0.25, 2.0)
            start = rng.randrange(0, sample_rate * 60)
            end = start + rng.randrange(1, sample_rate * 15)
            track = Track(
                1,
                "Matrix",
                start,
                end,
                start / sample_rate,
                end / sample_rate,
            )
            asetrate_hz, _ = _speed_correction_details(sample_rate, source_speed_factor)
            expected = _expected_track_sample_count(
                track, sample_rate, source_speed_factor
            )
            self.assertEqual(
                expected,
                _speed_corrected_sample(end, sample_rate, asetrate_hz)
                - _speed_corrected_sample(start, sample_rate, asetrate_hz),
            )

            marker = rng.randrange(start, end + 1)
            left = Track(
                1,
                "Left",
                start,
                marker,
                start / sample_rate,
                marker / sample_rate,
            )
            right = Track(
                1,
                "Right",
                marker,
                end,
                marker / sample_rate,
                end / sample_rate,
            )
            self.assertEqual(
                _expected_track_sample_count(left, sample_rate, source_speed_factor)
                + _expected_track_sample_count(right, sample_rate, source_speed_factor),
                expected,
            )

    def test_unknown_source_precision_is_not_forced_to_s16(self) -> None:
        track = Track(1, "One", 0, 48_000, 0.0, 1.0)
        with mock.patch("groove_serpent.exporter.find_tool", return_value="ffmpeg"):
            command = _build_command(
                source_path=Path("source.bin"),
                output_path=Path("track.flac"),
                track=track,
                total_tracks=1,
                output_format="flac",
                source_sample_rate=48_000,
                source_bits=None,
                overwrite=True,
                flac_compression=8,
                aac_bitrate="256k",
            )
        self.assertNotIn("-sample_fmt", command)

    def test_command_without_artwork_remains_audio_only(self) -> None:
        track = Track(1, "One", 0, 48_000, 0.0, 1.0)
        with mock.patch("groove_serpent.exporter.find_tool", return_value="ffmpeg"):
            command = _build_command(
                source_path=Path("source.flac"),
                output_path=Path("track.flac"),
                track=track,
                total_tracks=1,
                output_format="flac",
                source_sample_rate=48_000,
                source_bits=24,
                overwrite=True,
                flac_compression=8,
                aac_bitrate="256k",
            )

        inputs = [
            command[index + 1] for index, value in enumerate(command) if value == "-i"
        ]
        mappings = [
            command[index + 1] for index, value in enumerate(command) if value == "-map"
        ]
        self.assertEqual(inputs, ["source.flac"])
        self.assertEqual(mappings, ["0:a:0"])
        self.assertIn("-vn", command)
        self.assertNotIn("-c:v", command)

    def test_artwork_is_mapped_as_an_attached_picture_for_each_format(self) -> None:
        track = Track(1, "One", 0, 48_000, 0.0, 1.0)
        for output_format in ("flac", "m4a"):
            with self.subTest(output_format=output_format):
                with mock.patch(
                    "groove_serpent.exporter.find_tool", return_value="ffmpeg"
                ):
                    command = _build_command(
                        source_path=Path("source.flac"),
                        output_path=Path(f"track.{output_format}"),
                        track=track,
                        total_tracks=1,
                        output_format=output_format,
                        source_sample_rate=48_000,
                        source_bits=24,
                        overwrite=True,
                        flac_compression=8,
                        aac_bitrate="256k",
                        artwork_path=Path("artwork/cover.jpg"),
                    )

                inputs = [
                    command[index + 1]
                    for index, value in enumerate(command)
                    if value == "-i"
                ]
                mappings = [
                    command[index + 1]
                    for index, value in enumerate(command)
                    if value == "-map"
                ]
                self.assertEqual(
                    inputs, ["source.flac", str(Path("artwork/cover.jpg"))]
                )
                self.assertEqual(mappings, ["0:a:0", "1:v:0"])
                self.assertNotIn("-vn", command)
                self.assertEqual(command[command.index("-c:v") + 1], "copy")
                self.assertEqual(
                    command[command.index("-disposition:v:0") + 1], "attached_pic"
                )
                self.assertIn("title=Album cover", command)
                self.assertIn("comment=Cover (front)", command)

    def test_export_rejects_precision_above_flac_capacity_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            project = self._project(source_path, bits=32, rate=48_000)
            with mock.patch(
                "groove_serpent.exporter.probe_audio", return_value=project.source
            ):
                with self.assertRaisesRegex(ExportError, "above 24 bits"):
                    export_project(
                        project,
                        directory / "source.groove.json",
                        directory / "exports",
                        formats=["flac"],
                    )
            self.assertFalse((directory / "exports").exists())

    def test_export_rejects_high_rate_aac_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            project = self._project(source_path, bits=24, rate=192_000)
            with mock.patch(
                "groove_serpent.exporter.probe_audio", return_value=project.source
            ):
                with self.assertRaisesRegex(ExportError, "up to 96 kHz"):
                    export_project(
                        project,
                        directory / "source.groove.json",
                        directory / "exports",
                        formats=["m4a"],
                    )
            self.assertFalse((directory / "exports").exists())

    def test_export_rejects_same_shape_source_with_different_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            project = self._project(source_path, bits=24, rate=48_000)
            changed = replace(project.source, sha256="b" * 64)
            with mock.patch(
                "groove_serpent.exporter.probe_audio", return_value=changed
            ):
                with self.assertRaisesRegex(ExportError, "no longer matches"):
                    export_project(
                        project,
                        directory / "source.groove.json",
                        directory / "exports",
                        formats=["flac"],
                    )
            self.assertFalse((directory / "exports").exists())

    def test_matching_hash_allows_a_changed_source_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            project = self._project(source_path, bits=24, rate=48_000)
            touched = replace(
                project.source,
                modified_ns=project.source.modified_ns + 1_000_000,
                sha256=project.source.sha256,
            )

            def fake_ffmpeg(command):
                Path(list(command)[-1]).write_bytes(b"staged track")

            with (
                mock.patch("groove_serpent.exporter.probe_audio", return_value=touched),
                mock.patch(
                    "groove_serpent.exporter.run_ffmpeg", side_effect=fake_ffmpeg
                ),
            ):
                report = export_project(
                    project,
                    directory / "source.groove.json",
                    directory / "exports",
                    formats=["flac"],
                )
            self.assertEqual(len(report.files), 1)

    def test_new_batch_is_complete_before_one_atomic_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            project = self._project(source_path, bits=24, rate=48_000)
            output_dir = directory / "library" / "new-batch"
            real_rename = os.rename
            observed: dict[str, object] = {}

            def fake_ffmpeg(command: list[str]) -> None:
                destination = Path(command[-1])
                destination.write_bytes(f"audio:{destination.name}".encode())

            def publish(stage_value: str | Path, destination_value: str | Path) -> None:
                stage = Path(stage_value)
                destination = Path(destination_value)
                observed["stage"] = stage
                observed["destination"] = destination
                observed["names"] = sorted(path.name for path in stage.iterdir())
                staged_manifest = json.loads(
                    (stage / "groove-serpent-manifest.json").read_text(encoding="utf-8")
                )
                observed["manifest_paths"] = sorted(
                    item["path"] for item in staged_manifest["files"]
                )
                self.assertTrue(
                    all(
                        (stage / item["path"]).is_file()
                        for item in staged_manifest["files"]
                    )
                )
                real_rename(stage, destination)

            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio", return_value=project.source
                ),
                mock.patch(
                    "groove_serpent.exporter.run_ffmpeg", side_effect=fake_ffmpeg
                ),
                mock.patch(
                    "groove_serpent.exporter._probe_m4a_presentation_sample_count",
                    return_value=48_000,
                ),
                mock.patch(
                    "groove_serpent.exporter.os.rename", side_effect=publish
                ) as rename_call,
            ):
                report = export_project(
                    project,
                    directory / "source.groove.json",
                    output_dir,
                    formats=["flac", "m4a"],
                )

            rename_call.assert_called_once()
            resolved_output = output_dir.resolve()
            self.assertEqual(observed["destination"], resolved_output)
            self.assertEqual(Path(observed["stage"]).parent, resolved_output.parent)
            self.assertEqual(
                observed["names"],
                [
                    "01 - One.flac",
                    "01 - One.m4a",
                    "groove-serpent-manifest.json",
                ],
            )
            self.assertEqual(
                observed["manifest_paths"], ["01 - One.flac", "01 - One.m4a"]
            )
            self.assertTrue(output_dir.is_dir())
            self.assertEqual(
                Path(report.manifest_path),
                resolved_output / "groove-serpent-manifest.json",
            )
            self.assertEqual(
                list(output_dir.parent.glob(".groove-serpent-export-*.partial")), []
            )
            manifest = json.loads(
                Path(report.manifest_path).read_text(encoding="utf-8")
            )
            for item in manifest["files"]:
                self.assertEqual(item["expected_sample_count"], 48_000)
            self.assertNotIn("presentation_sample_count", manifest["files"][0])
            self.assertEqual(manifest["files"][1]["presentation_sample_count"], 48_000)

    def test_m4a_presentation_mismatch_fails_the_staged_batch_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            project = self._project(source_path, bits=24, rate=48_000)
            output_dir = directory / "new-batch"

            def fake_ffmpeg(command: list[str]) -> None:
                Path(command[-1]).write_bytes(b"staged m4a")

            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio", return_value=project.source
                ),
                mock.patch(
                    "groove_serpent.exporter.run_ffmpeg", side_effect=fake_ffmpeg
                ),
                mock.patch(
                    "groove_serpent.exporter._probe_m4a_presentation_sample_count",
                    return_value=47_999,
                ),
            ):
                with self.assertRaisesRegex(
                    ExportError, "47999 presentation samples; expected exactly 48000"
                ):
                    export_project(
                        project,
                        directory / "source.groove.json",
                        output_dir,
                        formats=["m4a"],
                    )

            self.assertFalse(output_dir.exists())
            self.assertEqual(
                list(directory.glob(".groove-serpent-export-*.partial")), []
            )

    def test_corrected_m4a_verification_uses_mapped_boundary_difference(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            project = self._project(source_path, bits=24, rate=48_000)

            def fake_ffmpeg(command: list[str]) -> None:
                Path(command[-1]).write_bytes(b"corrected m4a")

            expected = 49_872
            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio", return_value=project.source
                ),
                mock.patch(
                    "groove_serpent.exporter.run_ffmpeg", side_effect=fake_ffmpeg
                ),
                mock.patch(
                    "groove_serpent.exporter._probe_m4a_presentation_sample_count",
                    return_value=expected,
                ) as presentation_probe,
            ):
                report = export_project(
                    project,
                    directory / "source.groove.json",
                    directory / "corrected",
                    formats=["m4a"],
                    source_speed_factor=1.039,
                )

            presentation_probe.assert_called_once()
            self.assertEqual(report.files[0].expected_sample_count, expected)
            self.assertEqual(report.files[0].presentation_sample_count, expected)
            manifest = json.loads(
                Path(report.manifest_path).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["files"][0]["expected_sample_count"], expected)
            self.assertEqual(
                manifest["files"][0]["presentation_sample_count"], expected
            )

    def test_side_project_preserves_album_wide_track_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            project = self._project(source_path, bits=24, rate=48_000)
            project.tracks[0].side = "B"
            project.metadata.update(
                {
                    "track_number_offset": "6",
                    "album_track_total": "11",
                }
            )
            observed_commands: list[list[str]] = []

            def fake_ffmpeg(command: list[str]) -> None:
                observed_commands.append(command)
                Path(command[-1]).write_bytes(b"staged audio")

            output_dir = directory / "side-b-export"
            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio", return_value=project.source
                ),
                mock.patch(
                    "groove_serpent.exporter.run_ffmpeg", side_effect=fake_ffmpeg
                ),
            ):
                report = export_project(
                    project,
                    directory / "side-b.groove.json",
                    output_dir,
                    formats=["flac"],
                )

            self.assertEqual(report.files[0].track_number, 7)
            self.assertEqual(report.files[0].path, "07 - One.flac")
            self.assertIn("track=7/11", observed_commands[0])
            self.assertIn("tracktotal=11", observed_commands[0])
            manifest = json.loads(
                Path(report.manifest_path).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["tracks"], 1)
            self.assertEqual(manifest["album_tracks"], 11)
            self.assertEqual(manifest["track_number_offset"], 6)
            self.assertEqual(manifest["files"][0]["track_number"], 7)

    def test_invalid_album_wide_track_numbering_is_rejected_before_encoding(
        self,
    ) -> None:
        cases = (
            ({"track_number_offset": "-1"}, "whole number"),
            ({"album_track_total": "0"}, "between 1 and 9999"),
            (
                {"track_number_offset": "6", "album_track_total": "6"},
                "extends past",
            ),
        )
        for metadata, expected in cases:
            with (
                self.subTest(metadata=metadata),
                tempfile.TemporaryDirectory() as directory_value,
            ):
                directory = Path(directory_value)
                source_path = directory / "source.flac"
                source_path.write_bytes(b"source")
                project = self._project(source_path, bits=24, rate=48_000)
                project.metadata.update(metadata)
                with (
                    mock.patch(
                        "groove_serpent.exporter.probe_audio",
                        return_value=project.source,
                    ),
                    mock.patch("groove_serpent.exporter.run_ffmpeg") as ffmpeg,
                ):
                    with self.assertRaisesRegex(ExportError, expected):
                        export_project(
                            project,
                            directory / "side.groove.json",
                            directory / "export",
                            formats=["flac"],
                        )
                ffmpeg.assert_not_called()

    def test_speed_correction_manifest_is_explicit_and_invalid_factors_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            project = self._project(source_path, bits=24, rate=48_000)

            def fake_ffmpeg(command: list[str]) -> None:
                Path(command[-1]).write_bytes(b"speed-corrected audio")

            output_dir = directory / "corrected"
            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio", return_value=project.source
                ),
                mock.patch(
                    "groove_serpent.exporter.run_ffmpeg", side_effect=fake_ffmpeg
                ),
            ):
                report = export_project(
                    project,
                    directory / "source.groove.json",
                    output_dir,
                    formats=["flac"],
                    source_speed_factor=1.039,
                )
            manifest = json.loads(
                Path(report.manifest_path).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["speed_correction"]["source_speed_factor"], 1.039)
            self.assertEqual(manifest["speed_correction"]["output_sample_rate"], 48_000)
            self.assertEqual(manifest["speed_correction"]["asetrate_hz"], 46_198)
            self.assertAlmostEqual(
                manifest["speed_correction"]["effective_source_speed_factor"],
                48_000 / 46_198,
            )
            self.assertIn("soxr", manifest["speed_correction"]["method"])
            self.assertIn(
                "round-half-up",
                manifest["speed_correction"]["boundary_mapping"],
            )

            for index, invalid in enumerate(
                (True, float("nan"), 0.249, 2.01, 10**400)
            ):
                with (
                    self.subTest(invalid=invalid),
                    mock.patch(
                        "groove_serpent.exporter.probe_audio",
                        return_value=project.source,
                    ),
                    mock.patch("groove_serpent.exporter.run_ffmpeg") as ffmpeg,
                ):
                    with self.assertRaisesRegex(ExportError, "source speed factor"):
                        export_project(
                            project,
                            directory / "source.groove.json",
                            directory / f"invalid-{index}",
                            formats=["flac"],
                            source_speed_factor=invalid,
                        )
                ffmpeg.assert_not_called()

    def test_publish_failure_is_wrapped_and_cleans_the_staging_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            project = self._project(source_path, bits=24, rate=48_000)
            output_dir = directory / "library" / "new-batch"

            def fake_ffmpeg(command: list[str]) -> None:
                Path(command[-1]).write_bytes(b"staged audio")

            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio", return_value=project.source
                ),
                mock.patch(
                    "groove_serpent.exporter.run_ffmpeg", side_effect=fake_ffmpeg
                ),
                mock.patch(
                    "groove_serpent.exporter.os.rename",
                    side_effect=OSError("injected publish failure"),
                ),
            ):
                with self.assertRaisesRegex(
                    ExportError,
                    "complete batch could be published.*injected publish failure",
                ):
                    export_project(
                        project,
                        directory / "source.groove.json",
                        output_dir,
                        formats=["flac"],
                    )

            self.assertFalse(output_dir.exists())
            self.assertEqual(
                list(output_dir.parent.glob(".groove-serpent-export-*.partial")), []
            )

    def test_staging_failure_is_wrapped_and_leaves_no_visible_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            project = self._project(source_path, bits=24, rate=48_000)
            output_dir = directory / "new-batch"

            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio", return_value=project.source
                ),
                mock.patch(
                    "groove_serpent.exporter.run_ffmpeg",
                    side_effect=OSError("injected encoder failure"),
                ),
            ):
                with self.assertRaisesRegex(
                    ExportError,
                    "complete batch could be published.*injected encoder failure",
                ):
                    export_project(
                        project,
                        directory / "source.groove.json",
                        output_dir,
                        formats=["flac"],
                    )

            self.assertFalse(output_dir.exists())
            self.assertEqual(
                list(directory.glob(".groove-serpent-export-*.partial")), []
            )

    def test_case_insensitive_duplicate_export_names_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            project = self._project(source_path, bits=24, rate=48_000)
            output_dir = directory / "new-batch"

            def colliding_path(root: Path, _track: Track, extension: str) -> Path:
                return root / ("Same.FLAC" if extension == "flac" else "same.flac")

            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio", return_value=project.source
                ),
                mock.patch(
                    "groove_serpent.exporter._output_path", side_effect=colliding_path
                ),
                mock.patch("groove_serpent.exporter.run_ffmpeg") as ffmpeg,
            ):
                with self.assertRaisesRegex(
                    ExportError, "case-insensitive filesystems"
                ):
                    export_project(
                        project,
                        directory / "source.groove.json",
                        output_dir,
                        formats=["flac", "m4a"],
                    )

            ffmpeg.assert_not_called()
            self.assertFalse(output_dir.exists())
            self.assertEqual(
                list(directory.glob(".groove-serpent-export-*.partial")), []
            )

    def test_export_refuses_to_replace_the_source_with_a_track(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "01 - One.flac"
            original = b"immutable source"
            source_path.write_bytes(original)
            project = self._project(source_path, bits=24, rate=48_000)
            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio", return_value=project.source
                ),
                mock.patch("groove_serpent.exporter.run_ffmpeg") as ffmpeg,
            ):
                with self.assertRaisesRegex(
                    ExportError, "output directory already exists"
                ):
                    export_project(
                        project,
                        directory / "side.groove.json",
                        directory,
                        formats=["flac"],
                        overwrite=True,
                    )
            self.assertEqual(source_path.read_bytes(), original)
            ffmpeg.assert_not_called()

    def test_export_refuses_to_replace_the_project_with_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            project_path = directory / "groove-serpent-manifest.json"
            original_project = b"project placeholder"
            project_path.write_bytes(original_project)
            project = self._project(source_path, bits=24, rate=48_000)
            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio", return_value=project.source
                ),
                mock.patch("groove_serpent.exporter.run_ffmpeg") as ffmpeg,
            ):
                with self.assertRaisesRegex(
                    ExportError, "output directory already exists"
                ):
                    export_project(
                        project,
                        project_path,
                        directory,
                        formats=["flac"],
                        overwrite=True,
                    )
            self.assertEqual(project_path.read_bytes(), original_project)
            ffmpeg.assert_not_called()

    def test_existing_output_directory_is_refused_even_with_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            output_dir = directory / "exports"
            output_dir.mkdir()
            manifest = output_dir / "groove-serpent-manifest.json"
            original_manifest = b"existing manifest"
            manifest.write_bytes(original_manifest)
            project = self._project(source_path, bits=24, rate=48_000)
            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio", return_value=project.source
                ),
                mock.patch("groove_serpent.exporter.run_ffmpeg") as ffmpeg,
            ):
                with self.assertRaisesRegex(ExportError, "already exist"):
                    export_project(
                        project,
                        directory / "side.groove.json",
                        output_dir,
                        formats=["flac"],
                        overwrite=True,
                    )
            self.assertEqual(manifest.read_bytes(), original_manifest)
            ffmpeg.assert_not_called()

    def test_export_rejects_artwork_traversal_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            project_root = directory / "project"
            project_root.mkdir()
            source_path = project_root / "source.flac"
            source_path.write_bytes(b"source")
            (directory / "outside.jpg").write_bytes(b"\xff\xd8\xffoutside")
            project = self._project(source_path, bits=24, rate=48_000)
            project.metadata["cover_art_path"] = "../outside.jpg"
            output_dir = project_root / "exports"

            with mock.patch("groove_serpent.exporter.probe_audio") as probe:
                with mock.patch("groove_serpent.exporter.run_ffmpeg") as run:
                    with self.assertRaisesRegex(
                        ExportError, "inside the project folder"
                    ):
                        export_project(
                            project,
                            project_root / "source.groove.json",
                            output_dir,
                            formats=["flac"],
                        )

            probe.assert_not_called()
            run.assert_not_called()
            self.assertFalse(output_dir.exists())

    def test_export_rejects_missing_artwork_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            project_root = Path(directory_value)
            source_path = project_root / "source.flac"
            source_path.write_bytes(b"source")
            project = self._project(source_path, bits=24, rate=48_000)
            project.metadata["cover_art_path"] = "artwork/missing.png"
            output_dir = project_root / "exports"

            with mock.patch("groove_serpent.exporter.probe_audio") as probe:
                with mock.patch("groove_serpent.exporter.run_ffmpeg") as run:
                    with self.assertRaisesRegex(ExportError, "does not exist"):
                        export_project(
                            project,
                            project_root / "source.groove.json",
                            output_dir,
                            formats=["flac"],
                        )

            probe.assert_not_called()
            run.assert_not_called()
            self.assertFalse(output_dir.exists())

    def test_export_rejects_artwork_whose_hash_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            project_root = Path(directory_value)
            source_path = project_root / "source.flac"
            source_path.write_bytes(b"source")
            artwork_dir = project_root / "artwork"
            artwork_dir.mkdir()
            (artwork_dir / "cover.jpg").write_bytes(b"\xff\xd8\xffreplacement")
            project = self._project(source_path, bits=24, rate=48_000)
            project.metadata["cover_art_path"] = "artwork/cover.jpg"
            project.metadata["cover_art_sha256"] = "0" * 64
            output_dir = project_root / "exports"

            with (
                mock.patch("groove_serpent.exporter.probe_audio") as probe,
                mock.patch("groove_serpent.exporter.run_ffmpeg") as run,
            ):
                with self.assertRaisesRegex(ExportError, "no longer matches"):
                    export_project(
                        project,
                        project_root / "source.groove.json",
                        output_dir,
                        formats=["flac"],
                    )
            probe.assert_not_called()
            run.assert_not_called()
            self.assertFalse(output_dir.exists())

    def test_export_manifest_records_valid_local_artwork(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            project_root = Path(directory_value)
            source_path = project_root / "source.flac"
            source_path.write_bytes(b"source")
            artwork_dir = project_root / "artwork"
            artwork_dir.mkdir()
            artwork_path = artwork_dir / "cover.jpg"
            artwork_bytes = b"\xff\xd8\xff\xe0cover-art"
            artwork_path.write_bytes(artwork_bytes)
            project = self._project(source_path, bits=24, rate=48_000)
            project.metadata["cover_art_path"] = "artwork/cover.jpg"

            def write_fake_export(command: list[str]) -> None:
                Path(command[-1]).write_bytes(b"exported audio")

            with mock.patch(
                "groove_serpent.exporter.probe_audio", return_value=project.source
            ):
                with mock.patch(
                    "groove_serpent.exporter.run_ffmpeg", side_effect=write_fake_export
                ):
                    report = export_project(
                        project,
                        project_root / "source.groove.json",
                        project_root / "exports",
                        formats=["flac"],
                    )

            manifest = json.loads(
                Path(report.manifest_path).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["artwork"]["path"], "artwork/cover.jpg")
            self.assertEqual(
                manifest["artwork"]["sha256"],
                hashlib.sha256(artwork_bytes).hexdigest(),
            )
            self.assertEqual(
                manifest["artwork"]["file_identity"]["size_bytes"],
                len(artwork_bytes),
            )

    def test_manifest_binds_project_inputs_toolchain_plan_and_verification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            project = self._project(source_path, bits=24, rate=48_000)
            project_path = directory / "source.groove.json"
            save_project(project, project_path)
            expected_project_sha256 = hashlib.sha256(
                project_path.read_bytes()
            ).hexdigest()

            def fake_ffmpeg(command: list[str]) -> None:
                Path(command[-1]).write_bytes(b"verified staged FLAC")

            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio", return_value=project.source
                ),
                mock.patch(
                    "groove_serpent.exporter.run_ffmpeg", side_effect=fake_ffmpeg
                ),
            ):
                report = export_project(
                    project,
                    project_path,
                    directory / "export",
                    formats=["flac"],
                    flac_compression=11,
                )

            manifest = json.loads(
                Path(report.manifest_path).read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["schema"], "groove-serpent.publication-manifest/1"
            )
            self.assertEqual(manifest["project_file_sha256"], expected_project_sha256)
            self.assertEqual(manifest["project_revision"], project.revision)
            self.assertEqual(manifest["editable_state_sha256"], project.state_sha256)
            self.assertEqual(
                manifest["source_sha256"], hashlib.sha256(b"source").hexdigest()
            )
            self.assertEqual(manifest["output_profile"]["name"], "archival")
            self.assertEqual(
                manifest["encoder_settings"]["flac"]["compression_level"], 11
            )
            self.assertIn("ffmpeg", manifest["toolchain"])
            self.assertIn("ffprobe", manifest["toolchain"])
            self.assertEqual(
                canonical_json_sha256(manifest["processing_plan"]),
                manifest["processing_plan_sha256"],
            )
            self.assertTrue(manifest["verification"]["all_outputs_fully_probed"])
            self.assertTrue(
                manifest["files"][0]["verification"]["complete_decode_verified"]
            )
            for identity in (manifest["project_identity"], manifest["source_identity"]):
                self.assertIn("file_identity", identity)
                self.assertIn("inode", identity["file_identity"])
                self.assertIn("status_changed_ns", identity["file_identity"])

    def test_source_replaced_between_tracks_fails_closed_and_uses_one_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source-a")
            original_mtime = source_path.stat().st_mtime_ns
            project = self._two_track_project(source_path)
            project_path = directory / "side.groove.json"
            save_project(project, project_path)
            inputs: list[Path] = []

            def fake_ffmpeg(command: list[str]) -> None:
                inputs.append(Path(command[command.index("-i") + 1]))
                Path(command[-1]).write_bytes(b"staged")
                if len(inputs) == 1:
                    source_path.write_bytes(b"source-b")
                    os.utime(source_path, ns=(original_mtime, original_mtime))

            output_dir = directory / "export"
            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio", return_value=project.source
                ),
                mock.patch(
                    "groove_serpent.exporter.run_ffmpeg", side_effect=fake_ffmpeg
                ),
            ):
                with self.assertRaisesRegex(
                    ExportError, "Source audio changed during export.*sha256"
                ):
                    export_project(project, project_path, output_dir, formats=["flac"])

            self.assertEqual(len(inputs), 2)
            self.assertEqual(inputs[0], inputs[1])
            self.assertNotEqual(inputs[0], source_path)
            self.assertIn(".operation-inputs", inputs[0].parts)
            self.assertFalse(output_dir.exists())
            self.assertEqual(
                list(directory.glob(".groove-serpent-export-*.partial")), []
            )

    def test_artwork_replaced_between_tracks_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            artwork_dir = directory / "artwork"
            artwork_dir.mkdir()
            artwork_path = artwork_dir / "cover.jpg"
            artwork_path.write_bytes(b"\xff\xd8\xffcover-a")
            original_mtime = artwork_path.stat().st_mtime_ns
            project = self._two_track_project(source_path)
            project.metadata["cover_art_path"] = "artwork/cover.jpg"
            project_path = directory / "side.groove.json"
            save_project(project, project_path)
            calls = 0

            def fake_ffmpeg(command: list[str]) -> None:
                nonlocal calls
                calls += 1
                Path(command[-1]).write_bytes(b"staged")
                if calls == 1:
                    artwork_path.write_bytes(b"\xff\xd8\xffcover-b")
                    os.utime(artwork_path, ns=(original_mtime, original_mtime))

            output_dir = directory / "export"
            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio", return_value=project.source
                ),
                mock.patch(
                    "groove_serpent.exporter.run_ffmpeg", side_effect=fake_ffmpeg
                ),
            ):
                with self.assertRaisesRegex(
                    ExportError, "Cover artwork changed during export.*sha256"
                ):
                    export_project(project, project_path, output_dir, formats=["flac"])
            self.assertEqual(calls, 2)
            self.assertFalse(output_dir.exists())
            self.assertEqual(
                list(directory.glob(".groove-serpent-export-*.partial")), []
            )

    def test_project_file_changed_between_tracks_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source_path = directory / "source.flac"
            source_path.write_bytes(b"source")
            project = self._two_track_project(source_path)
            project_path = directory / "side.groove.json"
            save_project(project, project_path)
            calls = 0

            def fake_ffmpeg(command: list[str]) -> None:
                nonlocal calls
                calls += 1
                Path(command[-1]).write_bytes(b"staged")
                if calls == 1:
                    project_path.write_bytes(project_path.read_bytes() + b" ")

            output_dir = directory / "export"
            with (
                mock.patch(
                    "groove_serpent.exporter.probe_audio", return_value=project.source
                ),
                mock.patch(
                    "groove_serpent.exporter.run_ffmpeg", side_effect=fake_ffmpeg
                ),
            ):
                with self.assertRaisesRegex(
                    ExportError, "Project file changed during export"
                ):
                    export_project(project, project_path, output_dir, formats=["flac"])
            self.assertEqual(calls, 2)
            self.assertFalse(output_dir.exists())
            self.assertEqual(
                list(directory.glob(".groove-serpent-export-*.partial")), []
            )


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and FFprobe are required for publication verification tests.",
)
class ExporterRealVerificationTests(unittest.TestCase):
    def _real_project(self, directory: Path) -> tuple[Project, Path]:
        source_path = directory / "capture.flac"
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=997:sample_rate=48000:duration=1",
                "-ac",
                "2",
                "-c:a",
                "flac",
                "-sample_fmt",
                "s32",
                str(source_path),
            ],
            check=True,
        )
        source = probe_audio(source_path, stored_path=source_path.name)
        self.assertEqual(source.bits_per_raw_sample, 24)
        assert source.sample_count is not None
        start = 113
        end = source.sample_count - 97
        project = Project(
            source=source,
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(
                music_start_seconds=start / source.sample_rate,
                music_end_seconds=end / source.sample_rate,
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=[
                Track(
                    1,
                    "Integrity",
                    start,
                    end,
                    start / source.sample_rate,
                    end / source.sample_rate,
                )
            ],
        )
        project_path = directory / "capture.groove.json"
        save_project(project, project_path)
        return project, project_path

    def test_real_outputs_are_fully_verified_and_archival_flac_is_pcm_exact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            project, project_path = self._real_project(directory)
            report = export_project(
                project,
                project_path,
                directory / "publication",
                formats=["flac", "m4a"],
            )
            manifest = json.loads(
                Path(report.manifest_path).read_text(encoding="utf-8")
            )
            files = {item["format"]: item for item in manifest["files"]}
            expected = project.tracks[0].end_sample - project.tracks[0].start_sample
            self.assertEqual(
                files["flac"]["verification"]["exact_sample_count"], expected
            )
            self.assertEqual(files["flac"]["verification"]["bits_per_raw_sample"], 24)
            self.assertTrue(files["flac"]["verification"]["archival_pcm_equal"])
            self.assertEqual(
                files["flac"]["verification"]["decoded_pcm_sha256"],
                files["flac"]["verification"]["source_range_pcm_sha256"],
            )
            self.assertEqual(files["m4a"]["presentation_sample_count"], expected)
            self.assertTrue(files["m4a"]["verification"]["complete_decode_verified"])
            self.assertFalse(
                (Path(report.output_directory) / ".operation-inputs").exists()
            )

    def test_real_shellac_speed_factor_below_previous_limit_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            project, project_path = self._real_project(directory)
            source_speed_factor = (100.0 / 3.0) / 78.26
            self.assertGreater(source_speed_factor, 0.25)
            self.assertLess(source_speed_factor, 0.5)
            report = export_project(
                project,
                project_path,
                directory / "shellac-corrected",
                formats=["flac"],
                source_speed_factor=source_speed_factor,
            )
            expected = _expected_track_sample_count(
                project.tracks[0], project.source.sample_rate, source_speed_factor
            )
            manifest = json.loads(
                Path(report.manifest_path).read_text(encoding="utf-8")
            )
            self.assertEqual(report.files[0].expected_sample_count, expected)
            self.assertEqual(
                manifest["files"][0]["verification"]["exact_sample_count"], expected
            )
            self.assertEqual(manifest["output_profile"]["name"], "speed-corrected")
            self.assertAlmostEqual(
                manifest["speed_correction"]["source_speed_factor"],
                source_speed_factor,
            )


if __name__ == "__main__":
    unittest.main()
