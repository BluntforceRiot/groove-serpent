from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

import numpy as np

from groove_serpent import __version__
from groove_serpent.album import (
    AlbumProject,
    AlbumSide,
    AlbumSpeed,
    SpeedState,
    load_album_project,
    pin_album_side,
    project_speed_state,
    save_album_project,
)
from groove_serpent.album_publication_builder import (
    build_album_publication_plan,
    default_restoration_workspace,
)
from groove_serpent.album_publication_executor import (
    _ExecutionLease,
    _atomic_no_replace_directory,
    _assert_live_lease,
    _cleanup_stage,
    _directory_identity,
    _resolve_plan_reference,
    _verify_stage_tree,
    _walk_regular_files,
    execute_album_publication_plan,
    preflight_album_publication_plan,
)
from groove_serpent.album_publication_policy import (
    PublicationSettings,
    speed_correction_details,
)
from groove_serpent.errors import ExportError
from groove_serpent.exporter import _probe_exact_audio_stream, _speed_corrected_sample
from groove_serpent.media import probe_audio
from groove_serpent.models import AnalysisSettings, AnalysisSummary, Project, Track
from groove_serpent.project_io import load_project_with_sha256, save_project
from groove_serpent.restoration_workflow import SCAN_SCHEMA, _detector_manifest
from groove_serpent.restoration_workflow import (
    create_restoration_recipe,
    render_restored_side,
    scan_project_clicks,
)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and ffprobe are required",
)
class AlbumPublicationExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _write_album(self, *, speed_factor: float = 1.04) -> tuple[Path, Path]:
        source_path = self.root / "side-a.flac"
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
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
                "s16",
                str(source_path),
            ],
            check=True,
        )
        source = probe_audio(source_path, stored_path=source_path.name)
        project = Project(
            source=source,
            settings=AnalysisSettings(min_track_seconds=0.05),
            analysis=AnalysisSummary(
                music_start_seconds=0.1,
                music_end_seconds=0.9,
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=[
                Track(1, "First", 4_800, 24_000, 0.1, 0.5),
                Track(2, "Second", 24_000, 43_200, 0.5, 0.9),
            ],
            metadata={"artist": "Side Artist", "album": "Side Album"},
        )
        project_path = self.root / "side-a.groove.json"
        save_project(project, project_path)
        side = AlbumSide(
            "A",
            1,
            project_path.name,
            speed=AlbumSpeed.create(
                "override",
                SpeedState(fine_factor=speed_factor),
                project_speed_state(project),
            ),
        )
        album_path = self.root / "album.groove-album.json"
        pin_album_side(side, album_path)
        save_album_project(
            AlbumProject(
                metadata={"artist": "Album Artist", "album": "Test Album"},
                sides=[side],
            ),
            album_path,
        )
        return album_path, source_path

    def _build(
        self,
        album_path: Path,
        profiles: tuple[str, ...],
    ) -> Path:
        plan_path = self.root / "publication-plan.json"
        build_album_publication_plan(
            album_path,
            plan_path,
            selected_profiles=profiles,
            restoration_mode="none",
        )
        return plan_path

    def _add_second_side(
        self,
        album_path: Path,
        *,
        shared_source: bool,
    ) -> tuple[Path, Path]:
        first_project, _sha256 = load_project_with_sha256(self.root / "side-a.groove.json")
        if shared_source:
            source_path = self.root / first_project.source.filename
            source = first_project.source
        else:
            source_path = self.root / "side-b.flac"
            subprocess.run(
                [
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=431:sample_rate=48000:duration=1",
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
        tracks = [Track.from_dict(asdict(track)) for track in first_project.tracks]
        for track in tracks:
            track.title = f"B {track.title}"
            track.side = "B"
        second_project = Project(
            source=source,
            settings=first_project.settings,
            analysis=first_project.analysis,
            tracks=tracks,
            metadata={**first_project.metadata, "side": "B"},
        )
        second_project_path = self.root / "side-b.groove.json"
        save_project(second_project, second_project_path)
        album = load_album_project(album_path)
        second_side = AlbumSide("B", 2, second_project_path.name)
        pin_album_side(second_side, album_path)
        album.sides.append(second_side)
        save_album_project(album, album_path, overwrite=True)
        return second_project_path, source_path

    def _write_clean_scan(
        self,
        project_path: Path,
        *,
        name_digit: str = "5",
        created_at: str = "2026-07-13T00:00:00Z",
    ) -> Path:
        project, project_sha256 = load_project_with_sha256(project_path)
        workspace = default_restoration_workspace(project_path)
        workspace.mkdir(parents=True, exist_ok=True)
        scan_path = workspace / f"scan-{name_digit * 32}.json"
        start = project.tracks[0].start_sample
        end = project.tracks[-1].end_sample
        payload = {
            "schema": SCAN_SCHEMA,
            "created_at": created_at,
            "app_version": __version__,
            "project": {"path": project_path.name, "sha256": project_sha256},
            "source": {
                "path": project.source.filename,
                "sha256": project.source.sha256,
                "size_bytes": project.source.size_bytes,
                "sample_rate": project.source.sample_rate,
                "channels": project.source.channels,
                "bits_per_raw_sample": project.source.bits_per_raw_sample,
                "sample_count": project.source.sample_count,
            },
            "decoder": {
                "ffmpeg": "ffmpeg executor-test",
                "canonical_pcm": "s16le-interleaved",
                "bytes_per_frame": 4,
                "immutable_source_snapshot": True,
                "source_snapshot_sha256": project.source.sha256,
            },
            "detector": _detector_manifest(),
            "scan": {
                "start_frame": start,
                "end_frame_exclusive": end,
                "start_seconds": start / project.source.sample_rate,
                "end_seconds": end / project.source.sample_rate,
            },
            "candidates": [],
            "summary": {
                "detected": 0,
                "retained": 0,
                "truncated": False,
                "clipped": 0,
                "impulse": 0,
                "repairable": 0,
            },
            "coverage": {
                "music_start_frame": start,
                "music_end_frame_exclusive": end,
                "music_frame_count": end - start,
                "scanned_music_frames": end - start,
                "scanned_music_percent": 100.0,
                "scan_range_covers_music": True,
                "candidate_scan_truncated": False,
                "detected_candidates": 0,
                "retained_candidates": 0,
                "unretained_detections": 0,
                "unreviewed_regions": [],
                "restoration_status": "complete",
            },
        }
        scan_path.write_text(
            json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return scan_path

    def _write_click_render(self) -> tuple[Path, Path]:
        sample_rate = 44_100
        frame_count = 35_280
        click_start = 17_000
        time = np.arange(frame_count, dtype=np.float64) / sample_rate
        floating = np.column_stack(
            (
                0.24 * np.sin(2.0 * np.pi * 233.0 * time + 0.1),
                0.21 * np.sin(2.0 * np.pi * 311.0 * time + 0.2),
            )
        )
        pcm = np.rint(floating * 32_767.0).astype("<i2")
        pcm[click_start : click_start + 24, 0] = np.iinfo(np.int16).min
        source_path = self.root / "side-b.flac"
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
            settings=AnalysisSettings(min_track_seconds=0.05),
            analysis=AnalysisSummary(
                music_start_seconds=0.0,
                music_end_seconds=frame_count / sample_rate,
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=[
                Track(
                    1,
                    "Rendered repair",
                    0,
                    frame_count,
                    0.0,
                    frame_count / sample_rate,
                )
            ],
        )
        project_path = self.root / "side-b.groove.json"
        save_project(project, project_path)
        workspace = default_restoration_workspace(project_path)
        workspace.mkdir(parents=True)
        scan_path = workspace / f"scan-{'6' * 32}.json"
        scan = scan_project_clicks(project_path, scan_path, max_candidates=100)
        candidates = scan["candidates"]
        self.assertIsInstance(candidates, list)
        approved = next(
            item
            for item in candidates
            if item["repairable"]
            and item["start_frame"] < click_start + 24
            and item["end_frame_exclusive"] > click_start
        )
        decisions = [
            {
                "candidate_id": item["id"],
                "decision": "approved" if item["id"] == approved["id"] else "rejected",
            }
            for item in candidates
        ]
        recipe_path = workspace / f"recipe-{'7' * 32}.json"
        create_restoration_recipe(
            project_path,
            scan_path,
            decisions,
            recipe_path,
        )
        render_root = workspace / f"render-{'8' * 32}"
        render_restored_side(
            project_path,
            scan_path,
            recipe_path,
            render_root,
        )
        return project_path, render_root / "restored.flac"

    def test_read_only_preflight_revalidates_plan_without_creating_output(self) -> None:
        album_path, _source_path = self._write_album()
        plan_path = self._build(
            album_path,
            ("archival-source", "corrected-lossless", "portable"),
        )
        before = {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.root.rglob("*")
            if path.is_file()
        }

        report = preflight_album_publication_plan(plan_path)

        after = {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)
        self.assertEqual(report.side_count, 1)
        self.assertEqual(
            report.selected_profiles,
            ("archival-source", "corrected-lossless", "portable"),
        )
        self.assertRegex(report.plan_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(
            report.album_sha256,
            hashlib.sha256(album_path.read_bytes()).hexdigest(),
        )
        self.assertFalse(any(path.name.endswith(".partial") for path in self.root.iterdir()))

    def test_real_publication_rebases_corrects_then_splits_without_runout(self) -> None:
        album_path, source_path = self._write_album()
        plan_path = self._build(
            album_path,
            ("archival-source", "corrected-lossless", "portable"),
        )
        output = self.root / "published"

        report = execute_album_publication_plan(plan_path, output)

        self.assertEqual(Path(report.output_directory), output)
        manifest_text = Path(report.manifest_path).read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), manifest_text)
        manifest = json.loads(manifest_text)
        corrected = [
            item
            for item in manifest["inventory"]
            if item["profile"] == "corrected-lossless" and item["role"] == "corrected-track"
        ]
        self.assertEqual(len(corrected), 2)
        self.assertEqual(corrected[0]["relative_source_start_sample"], 0)
        self.assertEqual(
            corrected[0]["corrected_end_sample"],
            corrected[1]["corrected_start_sample"],
        )
        asetrate_hz, _effective = speed_correction_details(48_000, 1.04)
        expected_end = _speed_corrected_sample(38_400, 48_000, asetrate_hz)
        self.assertEqual(corrected[-1]["corrected_end_sample"], expected_end)
        self.assertEqual(
            sum(item["verification"]["exact_sample_count"] for item in corrected),
            expected_end,
        )
        for item in corrected:
            details = _probe_exact_audio_stream(output / item["path"])
            self.assertEqual(
                details["exact_sample_count"],
                item["corrected_end_sample"] - item["corrected_start_sample"],
            )
        portable = [item for item in manifest["inventory"] if item["profile"] == "portable"]
        self.assertEqual(len(portable), 2)
        self.assertTrue(
            all(item["encoded_from"] == "staged-corrected-lossless-flac" for item in portable)
        )
        archival = next(
            item for item in manifest["inventory"] if item["profile"] == "archival-source"
        )
        self.assertEqual(
            archival["sha256"],
            hashlib.sha256(source_path.read_bytes()).hexdigest(),
        )
        self.assertFalse(any((output / "provenance").rglob("*.flac")))
        chapters = json.loads((output / "album.chapters.json").read_text(encoding="utf-8"))
        self.assertEqual(chapters["basis_profile"], "corrected-lossless")
        self.assertEqual(chapters["total_tracks"], 2)
        self.assertEqual(
            chapters["sides"][0]["tracks"][-1]["side_output_end_sample_exclusive"],
            expected_end,
        )
        cue = (output / "album.cue").read_text(encoding="utf-8")
        self.assertEqual(cue.count("FILE "), 2)
        self.assertIn('INDEX_PRECISION "75 fps approximate', cue)
        self.assertEqual(
            {path.name for path in output.iterdir()},
            {
                "album.chapters.json",
                "album.cue",
                "archival-source",
                "artwork" if (output / "artwork").exists() else "provenance",
                "corrected-lossless",
                "portable",
                "provenance",
                "groove-serpent-album-publication.json",
                "groove-serpent-publication-journal.json",
            }
            - {"artwork"},
        )

    def test_shared_capture_materializes_one_object_with_two_exact_bindings(
        self,
    ) -> None:
        album_path, source_path = self._write_album(speed_factor=1.0)
        self._add_second_side(album_path, shared_source=True)
        plan_path = self._build(album_path, ("archival-source",))
        output = self.root / "published-shared"
        before = (
            source_path.read_bytes(),
            source_path.stat().st_mtime_ns,
        )

        report = execute_album_publication_plan(plan_path, output)

        manifest = json.loads(Path(report.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["schema"],
            "groove-serpent.album-publication-manifest/2",
        )
        archival = [item for item in manifest["inventory"] if item["role"] == "full-capture-source"]
        self.assertEqual(len(archival), 1)
        ledger = manifest["archival_sources"]
        self.assertEqual(len(ledger["objects"]), 1)
        self.assertEqual(len(ledger["side_bindings"]), 2)
        object_id = ledger["objects"][0]["object_id"]
        self.assertRegex(object_id, r"^source-01-[0-9a-f]{12}$")
        self.assertEqual(
            {item["source_object_id"] for item in ledger["side_bindings"]},
            {object_id},
        )
        self.assertEqual(
            [item["side_label"] for item in ledger["side_bindings"]],
            ["A", "B"],
        )
        self.assertEqual(ledger["objects"][0]["path"], archival[0]["path"])
        self.assertEqual(
            (output / archival[0]["path"]).read_bytes(),
            source_path.read_bytes(),
        )
        chapters = json.loads((output / "album.chapters.json").read_text(encoding="utf-8"))
        chapter_paths = {
            track["file"]["path"] for side in chapters["sides"] for track in side["tracks"]
        }
        self.assertEqual(chapter_paths, {archival[0]["path"]})
        self.assertEqual(
            before,
            (source_path.read_bytes(), source_path.stat().st_mtime_ns),
        )

    def test_distinct_captures_remain_distinct_archival_objects(self) -> None:
        album_path, first_source = self._write_album(speed_factor=1.0)
        _project, second_source = self._add_second_side(
            album_path,
            shared_source=False,
        )
        plan_path = self._build(album_path, ("archival-source",))
        output = self.root / "published-distinct"
        before = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (first_source, second_source)
        }

        execute_album_publication_plan(plan_path, output)

        manifest = json.loads(
            (output / "groove-serpent-album-publication.json").read_text(encoding="utf-8")
        )
        archival = [item for item in manifest["inventory"] if item["role"] == "full-capture-source"]
        objects = manifest["archival_sources"]["objects"]
        bindings = manifest["archival_sources"]["side_bindings"]
        self.assertEqual(len(archival), 2)
        self.assertEqual(len(objects), 2)
        self.assertEqual(len({item["path"] for item in objects}), 2)
        self.assertEqual(len({item["source_object_id"] for item in bindings}), 2)
        self.assertEqual(
            {item["source_sha256"] for item in objects},
            {
                hashlib.sha256(first_source.read_bytes()).hexdigest(),
                hashlib.sha256(second_source.read_bytes()).hexdigest(),
            },
        )
        self.assertEqual(
            before,
            {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (first_source, second_source)
            },
        )

    def test_portable_source_object_name_collision_fails_without_output(self) -> None:
        album_path, _source = self._write_album(speed_factor=1.0)
        self._add_second_side(album_path, shared_source=False)
        plan_path = self._build(album_path, ("archival-source",))
        output = self.root / "published-collision"

        with mock.patch(
            "groove_serpent.album_publication_executor._source_object_name",
            return_value="portable-collision.flac",
        ):
            with self.assertRaises(ExportError):
                execute_album_publication_plan(plan_path, output)

        self.assertFalse(output.exists())
        self.assertFalse(
            any(
                path.name.startswith(".groove-serpent-album-publication-")
                for path in self.root.iterdir()
            )
        )

    def test_shared_source_live_race_discards_deduplicated_stage(self) -> None:
        album_path, source_path = self._write_album(speed_factor=1.0)
        self._add_second_side(album_path, shared_source=True)
        plan_path = self._build(album_path, ("archival-source",))
        output = self.root / "published-live-race"

        def drift(boundary: str) -> None:
            if boundary == "after-manifest-verified":
                source_path.write_bytes(source_path.read_bytes() + b"late-drift")

        with self.assertRaisesRegex(ExportError, "changed during export"):
            execute_album_publication_plan(
                plan_path,
                output,
                fault_injector=drift,
            )

        self.assertFalse(output.exists())
        self.assertFalse(
            any(
                path.name.startswith(".groove-serpent-album-publication-")
                for path in self.root.iterdir()
            )
        )

    def test_shared_archival_object_stage_race_blocks_commit(self) -> None:
        album_path, source_path = self._write_album(speed_factor=1.0)
        self._add_second_side(album_path, shared_source=True)
        plan_path = self._build(album_path, ("archival-source",))
        output = self.root / "published-stage-race"
        source_before = source_path.read_bytes()

        def mutate(boundary: str) -> None:
            if boundary == "after-manifest-verified":
                stages = list(self.root.glob(".groove-serpent-album-publication-*.partial"))
                self.assertEqual(len(stages), 1)
                archival = next((stages[0] / "archival-source").iterdir())
                archival.write_bytes(archival.read_bytes() + b"stage-drift")

        with self.assertRaisesRegex(ExportError, "changed after inventory"):
            execute_album_publication_plan(
                plan_path,
                output,
                fault_injector=mutate,
            )

        self.assertFalse(output.exists())
        self.assertEqual(source_path.read_bytes(), source_before)
        self.assertFalse(
            any(
                path.name.startswith(".groove-serpent-album-publication-")
                for path in self.root.iterdir()
            )
        )

    def test_source_drift_fails_before_creating_output(self) -> None:
        album_path, source_path = self._write_album(speed_factor=1.0)
        plan_path = self._build(album_path, ("archival-source",))
        source_path.write_bytes(source_path.read_bytes() + b"drift")
        output = self.root / "published"

        with self.assertRaises(ExportError):
            execute_album_publication_plan(plan_path, output)

        self.assertFalse(output.exists())
        self.assertFalse(
            any(
                path.name.startswith(".groove-serpent-album-publication-")
                for path in self.root.iterdir()
            )
        )

    def test_archival_only_navigation_preserves_capture_but_indexes_music(self) -> None:
        album_path, _source_path = self._write_album(speed_factor=1.0)
        plan_path = self._build(album_path, ("archival-source",))
        output = self.root / "published-archival"

        execute_album_publication_plan(plan_path, output)

        chapters = json.loads((output / "album.chapters.json").read_text(encoding="utf-8"))
        self.assertEqual(chapters["basis_profile"], "archival-source")
        tracks = chapters["sides"][0]["tracks"]
        self.assertEqual(tracks[0]["file"]["start_sample"], 4_800)
        self.assertEqual(tracks[-1]["file"]["end_sample_exclusive"], 43_200)
        self.assertEqual(tracks[-1]["file"]["sample_count"], 48_000)
        cue = (output / "album.cue").read_text(encoding="utf-8")
        self.assertEqual(cue.count("FILE "), 1)

    def test_reviewed_clean_side_is_trimmed_and_published_pcm_equal(self) -> None:
        album_path, _source_path = self._write_album(speed_factor=1.0)
        self._write_clean_scan(self.root / "side-a.groove.json")
        plan_path = self.root / "publication-plan.json"
        build_album_publication_plan(
            album_path,
            plan_path,
            selected_profiles=("corrected-lossless",),
            restoration_mode="reviewed",
        )
        output = self.root / "published"

        execute_album_publication_plan(plan_path, output)

        manifest = json.loads(
            (output / "groove-serpent-album-publication.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["restoration_mode"], "reviewed")
        self.assertEqual(manifest["sides"][0]["restoration"]["outcome"], "clean")
        tracks = [
            item
            for item in manifest["inventory"]
            if item["profile"] == "corrected-lossless" and item["role"] == "corrected-track"
        ]
        self.assertEqual(
            sum(item["verification"]["exact_sample_count"] for item in tracks),
            38_400,
        )

    def test_mixed_render_and_reviewed_clean_album_publishes_both_outcomes(
        self,
    ) -> None:
        album_path, _source_path = self._write_album(speed_factor=1.0)
        render_project, restored_audio = self._write_click_render()
        album = load_album_project(album_path)
        rendered_side = AlbumSide("B", 2, render_project.name)
        pin_album_side(rendered_side, album_path)
        album.sides.append(rendered_side)
        save_album_project(album, album_path, overwrite=True)
        self._write_clean_scan(self.root / "side-a.groove.json")
        plan_path = self.root / "publication-plan.json"
        build_album_publication_plan(
            album_path,
            plan_path,
            selected_profiles=("restored-side", "corrected-lossless"),
            restoration_mode="reviewed",
        )
        output = self.root / "published"

        execute_album_publication_plan(plan_path, output)

        manifest = json.loads(
            (output / "groove-serpent-album-publication.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [side["restoration"]["outcome"] for side in manifest["sides"]],
            ["clean", "render"],
        )
        restored = [item for item in manifest["inventory"] if item["profile"] == "restored-side"]
        self.assertEqual(len(restored), 2)
        clean = next(item for item in restored if item["side_label"] == "A")
        rendered = next(item for item in restored if item["side_label"] == "B")
        self.assertTrue(clean["verification"]["reviewed_clean_pcm_equal"])
        self.assertEqual(
            rendered["sha256"],
            hashlib.sha256(restored_audio.read_bytes()).hexdigest(),
        )
        self.assertTrue(rendered["verification"]["validated_restoration_render"])

    def test_keyboard_interrupt_removes_only_private_stage(self) -> None:
        album_path, _source_path = self._write_album(speed_factor=1.0)
        plan_path = self._build(album_path, ("corrected-lossless",))
        output = self.root / "published"
        sentinel = self.root / "keep-me.txt"
        sentinel.write_text("safe", encoding="utf-8")

        with mock.patch(
            "groove_serpent.album_publication_executor.render_verified_track",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                execute_album_publication_plan(plan_path, output)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "safe")
        self.assertFalse(output.exists())
        self.assertFalse(
            any(
                path.name.startswith(".groove-serpent-album-publication-")
                for path in self.root.iterdir()
            )
        )

    def test_tree_verification_rejects_uninventoried_file(self) -> None:
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "groove-serpent-album-publication.json").write_text(
            "{}",
            encoding="utf-8",
        )
        (stage / "groove-serpent-publication-journal.json").write_text(
            "{}",
            encoding="utf-8",
        )
        (stage / "extra.txt").write_text("surprise", encoding="utf-8")

        with self.assertRaisesRegex(ExportError, "differs from its inventory"):
            _verify_stage_tree(stage, [])

    def test_atomic_commit_never_replaces_existing_empty_directory(self) -> None:
        source = self.root / "source-stage"
        destination = self.root / "existing-output"
        source.mkdir()
        destination.mkdir()
        (source / "source.txt").write_text("source", encoding="utf-8")

        with self.assertRaisesRegex(ExportError, "already exists"):
            _atomic_no_replace_directory(source, destination)

        self.assertEqual(
            (source / "source.txt").read_text(encoding="utf-8"),
            "source",
        )
        self.assertTrue(destination.is_dir())

    def test_cleanup_refuses_substituted_stage_directory(self) -> None:
        stage = self.root / (".groove-serpent-album-publication-" + "a" * 32 + ".partial")
        stage.mkdir()
        identity = _directory_identity(stage, label="Test stage")
        moved = self.root / "original-stage"
        stage.rename(moved)
        stage.mkdir()
        sentinel = stage / "do-not-delete.txt"
        sentinel.write_text("unowned", encoding="utf-8")

        with self.assertRaisesRegex(ExportError, "substituted"):
            _cleanup_stage(stage, self.root, identity)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unowned")
        self.assertTrue(moved.is_dir())

    def test_stage_walker_has_a_strict_depth_bound(self) -> None:
        root = self.root / "deep"
        root.mkdir()
        current = root
        for _index in range(34):
            current /= "d"
            current.mkdir()

        with self.assertRaisesRegex(ExportError, "nested too deeply"):
            _walk_regular_files(root)

    def test_final_component_symlink_is_never_resolved_as_input(self) -> None:
        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        link = self.root / "linked.json"
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"File symlinks are unavailable: {exc}")

        with self.assertRaisesRegex(ExportError, "non-reparse"):
            _resolve_plan_reference(
                self.root / "publication-plan.json",
                link.name,
                "Referenced input",
            )

    def test_newer_review_scan_invalidates_bound_clean_outcome(self) -> None:
        album_path, _source_path = self._write_album(speed_factor=1.0)
        project_path = self.root / "side-a.groove.json"
        self._write_clean_scan(project_path)
        plan_path = self.root / "publication-plan.json"
        build_album_publication_plan(
            album_path,
            plan_path,
            selected_profiles=("corrected-lossless",),
            restoration_mode="reviewed",
        )
        self._write_clean_scan(
            project_path,
            name_digit="9",
            created_at="2026-07-14T00:00:00Z",
        )
        output = self.root / "published"

        with self.assertRaisesRegex(ExportError, "latest current chain"):
            execute_album_publication_plan(plan_path, output)

        self.assertFalse(output.exists())

    def test_precommit_source_drift_discards_complete_staged_render(self) -> None:
        album_path, source_path = self._write_album(speed_factor=1.0)
        plan_path = self._build(album_path, ("corrected-lossless",))
        output = self.root / "published"

        def drift_then_assert(
            lease: _ExecutionLease,
            settings: PublicationSettings,
        ) -> None:
            source_path.write_bytes(source_path.read_bytes() + b"late-drift")
            _assert_live_lease(lease, settings)

        with mock.patch(
            "groove_serpent.album_publication_executor._assert_live_lease",
            side_effect=drift_then_assert,
        ):
            with self.assertRaisesRegex(ExportError, "changed during export"):
                execute_album_publication_plan(plan_path, output)

        self.assertFalse(output.exists())
        self.assertFalse(
            any(
                path.name.startswith(".groove-serpent-album-publication-")
                for path in self.root.iterdir()
            )
        )

    def test_stage_mutation_during_live_revalidation_blocks_commit(self) -> None:
        album_path, _source_path = self._write_album(speed_factor=1.0)
        plan_path = self._build(album_path, ("archival-source",))
        output = self.root / "published"

        def mutate_stage_after_live_check(
            lease: _ExecutionLease,
            settings: PublicationSettings,
        ) -> None:
            _assert_live_lease(lease, settings)
            stages = list(self.root.glob(".groove-serpent-album-publication-*.partial"))
            self.assertEqual(len(stages), 1)
            target = stages[0] / "provenance" / "publication-plan.json"
            target.write_bytes(target.read_bytes() + b"mutated")

        with mock.patch(
            "groove_serpent.album_publication_executor._assert_live_lease",
            side_effect=mutate_stage_after_live_check,
        ):
            with self.assertRaisesRegex(ExportError, "changed after inventory"):
                execute_album_publication_plan(plan_path, output)

        self.assertFalse(output.exists())
        self.assertFalse(
            any(
                path.name.startswith(".groove-serpent-album-publication-")
                for path in self.root.iterdir()
            )
        )

    def test_plan_path_symlink_is_rejected_before_staging(self) -> None:
        album_path, _source_path = self._write_album(speed_factor=1.0)
        plan_path = self._build(album_path, ("archival-source",))
        real_plan = self.root / "real-plan.json"
        plan_path.rename(real_plan)
        try:
            plan_path.symlink_to(real_plan)
        except OSError as exc:
            self.skipTest(f"File symlinks are unavailable: {exc}")
        output = self.root / "published"

        with self.assertRaisesRegex(ExportError, "non-reparse"):
            execute_album_publication_plan(plan_path, output)

        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
