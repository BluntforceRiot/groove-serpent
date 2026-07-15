from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from groove_serpent import __version__
from groove_serpent.album import (
    AlbumArtwork,
    AlbumProject,
    AlbumSide,
    load_album_project,
    pin_album_side,
    save_album_project,
)
from groove_serpent.album_publication_builder import (
    build_album_publication_plan,
    default_restoration_workspace,
)
from groove_serpent.album_publication_plan import (
    PROFILE_ARCHIVAL_SOURCE,
    PROFILE_CORRECTED_LOSSLESS,
    PROFILE_PORTABLE,
    PROFILE_RESTORED_SIDE,
    load_album_publication_plan,
)
from groove_serpent.album_publication_policy import (
    PublicationSettings,
    ToolObservations,
    operation_configuration,
    operation_tool_binding,
    speed_correction_details,
    validate_operation_tool_binding,
)
from groove_serpent.errors import ProjectValidationError
from groove_serpent.media import probe_audio, sha256_file
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.project_io import load_project_with_sha256, save_project
from groove_serpent.restoration_catalog import (
    RestorationArtifact,
    RestorationCatalog,
    RestorationCatalogIssue,
    RestorationDependency,
    RestorationFile,
)
from groove_serpent.restoration_workflow import (
    SCAN_SCHEMA,
    _detector_manifest,
    create_restoration_recipe,
    render_restored_side,
    scan_project_clicks,
)


_OBSERVATIONS = ToolObservations(
    groove_serpent_version=__version__,
    ffmpeg_version="ffmpeg version publication-test",
    ffprobe_version="ffprobe version publication-test",
    ffmpeg_executable_sha256="1" * 64,
    ffprobe_executable_sha256="2" * 64,
    ffmpeg_version_output_sha256="3" * 64,
    ffprobe_version_output_sha256="4" * 64,
)


class AlbumPublicationBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _write_project(self, stem: str, *, sample_rate: int = 1_000) -> Path:
        source_path = self.root / f"{stem}.flac"
        source_payload = (f"immutable-{stem}".encode("utf-8")) * 100
        source_path.write_bytes(source_payload)
        metadata = source_path.stat()
        sample_count = 2_000
        project = Project(
            source=AudioSource(
                path=source_path.name,
                filename=source_path.name,
                size_bytes=metadata.st_size,
                modified_ns=metadata.st_mtime_ns,
                duration_seconds=sample_count / sample_rate,
                sample_rate=sample_rate,
                channels=2,
                codec_name="flac",
                bits_per_raw_sample=16,
                sample_format="s16",
                sample_count=sample_count,
                sha256=hashlib.sha256(source_payload).hexdigest(),
            ),
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(
                music_start_seconds=0.0,
                music_end_seconds=sample_count / sample_rate,
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=[
                Track(
                    number=1,
                    title=f"{stem} track",
                    start_sample=0,
                    end_sample=sample_count,
                    start_seconds=0.0,
                    end_seconds=sample_count / sample_rate,
                )
            ],
        )
        project_path = self.root / f"{stem}.groove.json"
        save_project(project, project_path)
        return project_path

    def _write_album(self, stems: tuple[str, ...] = ("side-a",)) -> Path:
        album_path = self.root / "album.groove-album.json"
        sides: list[AlbumSide] = []
        for order, stem in enumerate(stems, start=1):
            project_path = self._write_project(stem)
            side = AlbumSide(chr(64 + order), order, project_path.name)
            pin_album_side(side, album_path)
            sides.append(side)
        save_album_project(
            AlbumProject(
                metadata={"artist": "Artist", "album": "Album"},
                sides=sides,
            ),
            album_path,
        )
        return album_path

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_zero_candidate_scan(
        self,
        project_path: Path,
        *,
        truncated: bool = False,
        stale: bool = False,
    ) -> Path:
        project, project_sha256 = load_project_with_sha256(project_path)
        source_path = self.root / project.source.path
        workspace = default_restoration_workspace(project_path)
        scan_path = workspace / f"scan-{'5' * 32}.json"
        detected = 1 if truncated else 0
        payload = {
            "schema": SCAN_SCHEMA,
            "created_at": "2026-07-13T00:00:00Z",
            "app_version": __version__,
            "project": {
                "path": project_path.name,
                "sha256": "0" * 64 if stale else project_sha256,
            },
            "source": {
                "path": source_path.name,
                "sha256": project.source.sha256,
                "size_bytes": project.source.size_bytes,
                "sample_rate": project.source.sample_rate,
                "channels": project.source.channels,
                "bits_per_raw_sample": project.source.bits_per_raw_sample,
                "sample_count": project.source.sample_count,
            },
            "decoder": {
                "ffmpeg": "ffmpeg publication-test",
                "canonical_pcm": "s16le-interleaved",
                "bytes_per_frame": 4,
                "immutable_source_snapshot": True,
                "source_snapshot_sha256": project.source.sha256,
            },
            "detector": _detector_manifest(),
            "scan": {
                "start_frame": 0,
                "end_frame_exclusive": 2_000,
                "start_seconds": 0.0,
                "end_seconds": 2.0,
            },
            "candidates": [],
            "summary": {
                "detected": detected,
                "retained": 0,
                "truncated": truncated,
                "clipped": 0,
                "impulse": 0,
                "repairable": 0,
            },
            "coverage": {
                "music_start_frame": 0,
                "music_end_frame_exclusive": 2_000,
                "music_frame_count": 2_000,
                "scanned_music_frames": 2_000,
                "scanned_music_percent": 100.0,
                "scan_range_covers_music": True,
                "candidate_scan_truncated": truncated,
                "detected_candidates": detected,
                "retained_candidates": 0,
                "unretained_detections": detected,
                "unreviewed_regions": [],
                "restoration_status": "partial" if truncated else "complete",
            },
        }
        self._write_json(scan_path, payload)
        return scan_path

    def _render_catalog(self, project_path: Path) -> RestorationCatalog:
        project, project_sha256 = load_project_with_sha256(project_path)
        source_path = self.root / project.source.path
        workspace = default_restoration_workspace(project_path)
        workspace.mkdir(parents=True, exist_ok=True)

        scan_path = workspace / f"scan-{'6' * 32}.json"
        recipe_path = workspace / f"recipe-{'7' * 32}.json"
        render_dir = workspace / f"render-{'8' * 32}"
        render_path = render_dir / "render.json"
        restored_path = render_dir / "restored.flac"
        self._write_json(scan_path, {"kind": "validated-scan"})
        self._write_json(recipe_path, {"kind": "validated-recipe"})
        self._write_json(render_path, {"kind": "validated-render"})
        restored_path.write_bytes(b"lossless-restored-side")

        scan_sha = sha256_file(scan_path)
        recipe_sha = sha256_file(recipe_path)
        render_sha = sha256_file(render_path)
        restored_sha = sha256_file(restored_path)
        scan_id = f"scan-{scan_sha[:32]}"
        recipe_id = f"recipe-{recipe_sha[:32]}"
        scan = RestorationArtifact(
            scan_id,
            "scan",
            scan_path,
            scan_sha,
            "2026-07-13T00:00:00Z",
            "2026-07-13T00:00:00.000000+00:00",
            {"coverage": {}, "summary": {}, "candidates": []},
        )
        scan_dependency = RestorationDependency(
            "scan", scan_path.name, scan_sha, scan_id
        )
        recipe = RestorationArtifact(
            recipe_id,
            "recipe",
            recipe_path,
            recipe_sha,
            "2026-07-13T00:01:00Z",
            "2026-07-13T00:01:00.000000+00:00",
            {},
            (scan_dependency,),
        )
        recipe_dependency = RestorationDependency(
            "recipe", recipe_path.name, recipe_sha, recipe_id
        )
        render = RestorationArtifact(
            f"render-{render_sha[:32]}",
            "render",
            render_path,
            render_sha,
            "2026-07-13T00:02:00Z",
            "2026-07-13T00:02:00.000000+00:00",
            {},
            (scan_dependency, recipe_dependency),
            (
                RestorationFile(
                    "restored",
                    restored_path.name,
                    restored_path,
                    restored_sha,
                    restored_path.stat().st_size,
                ),
            ),
        )
        return RestorationCatalog(
            workspace,
            project_path,
            project_sha256,
            source_path,
            project.source.sha256,
            (scan, recipe, render),
            (),
            (),
        )

    def _empty_catalog(
        self,
        project_path: Path,
        *,
        stale: tuple[RestorationArtifact, ...] = (),
        invalid: tuple[RestorationCatalogIssue, ...] = (),
    ) -> RestorationCatalog:
        project, project_sha256 = load_project_with_sha256(project_path)
        return RestorationCatalog(
            default_restoration_workspace(project_path),
            project_path,
            project_sha256,
            self.root / project.source.path,
            project.source.sha256,
            (),
            stale,
            invalid,
        )

    @patch(
        "groove_serpent.album_publication_builder.observe_publication_tools",
        return_value=_OBSERVATIONS,
    )
    def test_direct_source_portable_plan_is_complete_and_reproducible(
        self, _observe: object
    ) -> None:
        album_path = self._write_album()
        plan_path = self.root / "publication.json"
        plan = build_album_publication_plan(
            album_path,
            plan_path,
            selected_profiles=(PROFILE_PORTABLE,),
            restoration_mode="none",
        )

        self.assertEqual(
            plan.selected_profiles,
            (PROFILE_CORRECTED_LOSSLESS, PROFILE_PORTABLE),
        )
        self.assertIsNone(plan.sides[0].restoration_render)
        self.assertIsNone(plan.sides[0].restoration_no_derivative)
        self.assertEqual(plan, load_album_publication_plan(plan_path))
        self.assertEqual(
            {node.operation for node in plan.nodes},
            {
                "source-side",
                "correct-speed-side",
                "encode-lossless",
                "encode-portable",
            },
        )
        corrected = next(
            node for node in plan.nodes if node.operation == "correct-speed-side"
        )
        self.assertEqual(corrected.tool.configuration["restoration_mode"], "none")
        self.assertEqual(
            corrected.tool.configuration["input_mode"],
            "project-source-music-range",
        )

    @patch(
        "groove_serpent.album_publication_builder.observe_publication_tools",
        return_value=_OBSERVATIONS,
    )
    def test_archival_profile_is_source_only(self, _observe: object) -> None:
        album_path = self._write_album()
        plan = build_album_publication_plan(
            album_path,
            self.root / "archival.json",
            selected_profiles=(PROFILE_ARCHIVAL_SOURCE,),
            restoration_mode="none",
        )
        self.assertEqual(
            {node.operation for node in plan.nodes},
            {"source-side", "assemble-archival"},
        )

    def test_profile_and_restoration_mode_contract_is_strict(self) -> None:
        album_path = self._write_album()
        with self.assertRaisesRegex(ProjectValidationError, "bounded collection"):
            build_album_publication_plan(
                album_path,
                self.root / "string-profile.json",
                selected_profiles=PROFILE_PORTABLE,
                restoration_mode="none",
            )
        cases = (
            ((PROFILE_ARCHIVAL_SOURCE,), "reviewed", "does not consume"),
            ((PROFILE_RESTORED_SIDE,), "none", "requires"),
            ((PROFILE_CORRECTED_LOSSLESS,), "automatic", "exactly"),
            (("future-profile",), "none", "Unsupported"),
            (
                (PROFILE_CORRECTED_LOSSLESS, PROFILE_CORRECTED_LOSSLESS),
                "none",
                "only once",
            ),
        )
        for profiles, mode, message in cases:
            with self.subTest(profiles=profiles, mode=mode), self.assertRaisesRegex(
                ProjectValidationError, message
            ):
                build_album_publication_plan(
                    album_path,
                    self.root / f"bad-{len(message)}.json",
                    selected_profiles=profiles,
                    restoration_mode=mode,
                )

    @patch(
        "groove_serpent.album_publication_builder.observe_publication_tools",
        return_value=_OBSERVATIONS,
    )
    def test_reviewed_zero_candidate_scan_is_bound_for_corrected_profile(
        self, _observe: object
    ) -> None:
        album_path = self._write_album()
        project_path = self.root / "side-a.groove.json"
        scan_path = self._write_zero_candidate_scan(project_path)

        plan = build_album_publication_plan(
            album_path,
            self.root / "reviewed-clean.json",
            selected_profiles=(PROFILE_CORRECTED_LOSSLESS,),
            restoration_mode="reviewed",
        )

        outcome = plan.sides[0].restoration_no_derivative
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(outcome.scan_sha256, sha256_file(scan_path))
        self.assertIsNone(plan.sides[0].restoration_render)
        self.assertNotIn("restore-side", {node.operation for node in plan.nodes})

    @patch(
        "groove_serpent.album_publication_builder.observe_publication_tools",
        return_value=_OBSERVATIONS,
    )
    def test_restored_profile_rejects_all_clean_sides(self, _observe: object) -> None:
        album_path = self._write_album()
        self._write_zero_candidate_scan(self.root / "side-a.groove.json")
        with self.assertRaisesRegex(ProjectValidationError, "rendered derivative"):
            build_album_publication_plan(
                album_path,
                self.root / "restored-clean-only.json",
                selected_profiles=(PROFILE_RESTORED_SIDE,),
                restoration_mode="reviewed",
            )

    @patch(
        "groove_serpent.album_publication_builder.observe_publication_tools",
        return_value=_OBSERVATIONS,
    )
    def test_truncated_scan_is_not_an_approved_clean_outcome(
        self, _observe: object
    ) -> None:
        album_path = self._write_album()
        self._write_zero_candidate_scan(
            self.root / "side-a.groove.json", truncated=True
        )
        with self.assertRaisesRegex(ProjectValidationError, "not a complete"):
            build_album_publication_plan(
                album_path,
                self.root / "partial.json",
                selected_profiles=(PROFILE_CORRECTED_LOSSLESS,),
                restoration_mode="reviewed",
            )

    @patch(
        "groove_serpent.album_publication_builder.observe_publication_tools",
        return_value=_OBSERVATIONS,
    )
    def test_stale_and_missing_review_outcomes_fail_closed(
        self, _observe: object
    ) -> None:
        album_path = self._write_album()
        project_path = self.root / "side-a.groove.json"
        stale_path = self._write_zero_candidate_scan(project_path, stale=True)
        with self.assertRaisesRegex(ProjectValidationError, "no current reviewed"):
            build_album_publication_plan(
                album_path,
                self.root / "stale.json",
                selected_profiles=(PROFILE_CORRECTED_LOSSLESS,),
                restoration_mode="reviewed",
            )
        stale_path.unlink()
        with self.assertRaisesRegex(ProjectValidationError, "no current reviewed"):
            build_album_publication_plan(
                album_path,
                self.root / "missing.json",
                selected_profiles=(PROFILE_CORRECTED_LOSSLESS,),
                restoration_mode="reviewed",
            )

    @patch(
        "groove_serpent.album_publication_builder.observe_publication_tools",
        return_value=_OBSERVATIONS,
    )
    def test_mixed_repaired_and_reviewed_clean_sides_form_one_coherent_plan(
        self, _observe: object
    ) -> None:
        album_path = self._write_album(("side-a", "side-b"))
        projects = {
            name: self.root / f"{name}.groove.json"
            for name in ("side-a", "side-b")
        }
        render_catalog = self._render_catalog(projects["side-a"])
        self._write_zero_candidate_scan(projects["side-b"])
        from groove_serpent.restoration_catalog import discover_restoration_catalog

        clean_catalog = discover_restoration_catalog(
            default_restoration_workspace(projects["side-b"]),
            projects["side-b"],
        )
        catalogs = {
            projects["side-a"].resolve(): render_catalog,
            projects["side-b"].resolve(): clean_catalog,
        }

        def discover(
            _workspace: Path,
            project_path: Path,
            *,
            verified_source_sha256: str,
        ) -> RestorationCatalog:
            catalog = catalogs[project_path.resolve()]
            self.assertEqual(catalog.source_sha256, verified_source_sha256)
            return catalog

        with patch(
            "groove_serpent.album_publication_builder.discover_restoration_catalog",
            side_effect=discover,
        ):
            plan = build_album_publication_plan(
                album_path,
                self.root / "mixed.json",
                selected_profiles=(
                    PROFILE_RESTORED_SIDE,
                    PROFILE_CORRECTED_LOSSLESS,
                ),
                restoration_mode="reviewed",
            )

        self.assertIsNotNone(plan.sides[0].restoration_render)
        self.assertIsNotNone(plan.sides[1].restoration_no_derivative)
        self.assertEqual(
            [node.operation for node in plan.nodes].count("restore-side"), 1
        )
        restored_output = next(
            node for node in plan.nodes if node.operation == "assemble-restored"
        )
        self.assertEqual(len(restored_output.inputs), 2)

    @patch(
        "groove_serpent.album_publication_builder.observe_publication_tools",
        return_value=_OBSERVATIONS,
    )
    def test_partial_reviewed_album_branch_fails(self, _observe: object) -> None:
        album_path = self._write_album(("side-a", "side-b"))
        self._write_zero_candidate_scan(self.root / "side-a.groove.json")
        with self.assertRaisesRegex(ProjectValidationError, "Side 'B'.*no current"):
            build_album_publication_plan(
                album_path,
                self.root / "partial-album.json",
                selected_profiles=(PROFILE_CORRECTED_LOSSLESS,),
                restoration_mode="reviewed",
            )

    @patch(
        "groove_serpent.album_publication_builder.observe_publication_tools",
        return_value=_OBSERVATIONS,
    )
    def test_catalog_drift_is_detected_before_save(self, _observe: object) -> None:
        album_path = self._write_album()
        project_path = self.root / "side-a.groove.json"
        first = self._render_catalog(project_path)
        drifted = RestorationCatalog(
            first.workspace,
            first.project_path,
            first.project_sha256,
            first.source_path,
            first.source_sha256,
            first.artifacts,
            first.stale,
            (
                RestorationCatalogIssue(
                    first.workspace / "new.json",
                    None,
                    "new_entry",
                    "A new entry appeared.",
                ),
            ),
        )
        with patch(
            "groove_serpent.album_publication_builder.discover_restoration_catalog",
            side_effect=(first, drifted),
        ), self.assertRaisesRegex(ProjectValidationError, "workspace changed"):
            build_album_publication_plan(
                album_path,
                self.root / "catalog-drift.json",
                selected_profiles=(PROFILE_CORRECTED_LOSSLESS,),
                restoration_mode="reviewed",
            )

    def test_tool_drift_and_source_drift_are_detected_before_save(self) -> None:
        album_path = self._write_album()
        changed = ToolObservations(
            _OBSERVATIONS.groove_serpent_version,
            _OBSERVATIONS.ffmpeg_version,
            _OBSERVATIONS.ffprobe_version,
            "9" * 64,
            _OBSERVATIONS.ffprobe_executable_sha256,
            _OBSERVATIONS.ffmpeg_version_output_sha256,
            _OBSERVATIONS.ffprobe_version_output_sha256,
        )
        with patch(
            "groove_serpent.album_publication_builder.observe_publication_tools",
            side_effect=(_OBSERVATIONS, changed),
        ), self.assertRaisesRegex(ProjectValidationError, "tool binaries"):
            build_album_publication_plan(
                album_path,
                self.root / "tool-drift.json",
                selected_profiles=(PROFILE_ARCHIVAL_SOURCE,),
                restoration_mode="none",
            )

        source_path = self.root / "side-a.flac"
        calls = 0

        def mutate_source() -> ToolObservations:
            nonlocal calls
            calls += 1
            if calls == 2:
                source_path.write_bytes(source_path.read_bytes() + b"changed")
            return _OBSERVATIONS

        with patch(
            "groove_serpent.album_publication_builder.observe_publication_tools",
            side_effect=mutate_source,
        ), self.assertRaisesRegex(ProjectValidationError, "source changed"):
            build_album_publication_plan(
                album_path,
                self.root / "source-drift.json",
                selected_profiles=(PROFILE_ARCHIVAL_SOURCE,),
                restoration_mode="none",
            )

    @patch(
        "groove_serpent.album_publication_builder.observe_publication_tools",
        return_value=_OBSERVATIONS,
    )
    def test_destination_policy_is_beside_album_and_no_overwrite(
        self, _observe: object
    ) -> None:
        album_path = self._write_album()
        outside = self.root / "nested" / "plan.json"
        with self.assertRaisesRegex(
            ProjectValidationError, "parent folder|directly beside"
        ):
            build_album_publication_plan(
                album_path,
                outside,
                selected_profiles=(PROFILE_ARCHIVAL_SOURCE,),
                restoration_mode="none",
            )
        plan_path = self.root / "existing.json"
        plan_path.write_text("owner data", encoding="utf-8")
        with self.assertRaisesRegex(ProjectValidationError, "already exists"):
            build_album_publication_plan(
                album_path,
                plan_path,
                selected_profiles=(PROFILE_ARCHIVAL_SOURCE,),
                restoration_mode="none",
            )
        self.assertEqual(plan_path.read_text(encoding="utf-8"), "owner data")

    def test_destination_rejects_nonportable_final_components(self) -> None:
        album_path = self._write_album()
        invalid_names = (
            "CON.json",
            "lpt9.release.json",
            "bad?.json",
            "bad|name.json",
            "plan.json.",
            " plan.json",
            "Cafe\u0301.json",
            ".json",
        )
        for name in invalid_names:
            with self.subTest(name=name), self.assertRaisesRegex(
                ProjectValidationError, "canonical portable"
            ):
                build_album_publication_plan(
                    album_path,
                    self.root / name,
                    selected_profiles=(PROFILE_ARCHIVAL_SOURCE,),
                    restoration_mode="none",
                )

    def test_destination_rejects_portable_equivalent_sibling_and_race(self) -> None:
        album_path = self._write_album()
        decomposed = self.root / "Cafe\u0301.json"
        decomposed.write_text("owner data", encoding="utf-8")
        with self.assertRaisesRegex(ProjectValidationError, "portable-equivalent"):
            build_album_publication_plan(
                album_path,
                self.root / "Caf\u00e9.json",
                selected_profiles=(PROFILE_ARCHIVAL_SOURCE,),
                restoration_mode="none",
            )
        self.assertEqual(decomposed.read_text(encoding="utf-8"), "owner data")

        decomposed.unlink()
        calls = 0

        def appear_before_commit() -> ToolObservations:
            nonlocal calls
            calls += 1
            if calls == 2:
                decomposed.write_text("racing owner data", encoding="utf-8")
            return _OBSERVATIONS

        with patch(
            "groove_serpent.album_publication_builder.observe_publication_tools",
            side_effect=appear_before_commit,
        ), self.assertRaisesRegex(ProjectValidationError, "portable-equivalent"):
            build_album_publication_plan(
                album_path,
                self.root / "Caf\u00e9.json",
                selected_profiles=(PROFILE_ARCHIVAL_SOURCE,),
                restoration_mode="none",
            )
        self.assertEqual(
            decomposed.read_text(encoding="utf-8"), "racing owner data"
        )

    def test_final_album_symlink_is_rejected_without_following_it(self) -> None:
        album_path = self._write_album()
        linked_album = self.root / "linked-album.groove-album.json"
        try:
            linked_album.symlink_to(album_path)
        except OSError as exc:
            self.skipTest(f"Creating a test symlink is not permitted: {exc}")
        with self.assertRaisesRegex(ProjectValidationError, "non-reparse"):
            build_album_publication_plan(
                linked_album,
                self.root / "symlink-plan.json",
                selected_profiles=(PROFILE_ARCHIVAL_SOURCE,),
                restoration_mode="none",
            )
        self.assertFalse((self.root / "symlink-plan.json").exists())

    @patch(
        "groove_serpent.album_publication_builder.observe_publication_tools",
        return_value=_OBSERVATIONS,
    )
    def test_stale_album_pin_is_rejected(self, _observe: object) -> None:
        album_path = self._write_album()
        project_path = self.root / "side-a.groove.json"
        project, _sha = load_project_with_sha256(project_path)
        project.metadata["edited"] = "after album approval"
        save_project(project, project_path)
        with self.assertRaisesRegex(ProjectValidationError, "pin is stale"):
            build_album_publication_plan(
                album_path,
                self.root / "stale-pin.json",
                selected_profiles=(PROFILE_ARCHIVAL_SOURCE,),
                restoration_mode="none",
            )

    @patch(
        "groove_serpent.album_publication_builder.observe_publication_tools",
        return_value=_OBSERVATIONS,
    )
    def test_unpinned_side_and_drifted_artwork_fail_closed(
        self, _observe: object
    ) -> None:
        album_path = self._write_album()
        raw_album = json.loads(album_path.read_text(encoding="utf-8"))
        raw_album["sides"][0]["pin"] = None
        self._write_json(album_path, raw_album)
        with self.assertRaisesRegex(ProjectValidationError, "not approved and pinned"):
            build_album_publication_plan(
                album_path,
                self.root / "unpinned.json",
                selected_profiles=(PROFILE_ARCHIVAL_SOURCE,),
                restoration_mode="none",
            )

        album_path.unlink()
        album_path = self._write_album()
        album = load_album_project(album_path)
        artwork_path = self.root / "cover.png"
        artwork_path.write_bytes(b"approved-cover")
        album.artwork = AlbumArtwork(artwork_path.name, sha256_file(artwork_path))
        save_album_project(album, album_path, overwrite=True)
        artwork_path.write_bytes(b"changed-cover")
        with self.assertRaisesRegex(ProjectValidationError, "no longer matches"):
            build_album_publication_plan(
                album_path,
                self.root / "artwork-drift.json",
                selected_profiles=(PROFILE_ARCHIVAL_SOURCE,),
                restoration_mode="none",
            )

    def test_policy_binds_exact_speed_math_codecs_and_tool_identity(self) -> None:
        settings = PublicationSettings(flac_compression=11, aac_bitrate_kbps=320)
        asetrate, effective = speed_correction_details(96_000, 1.04)
        self.assertEqual(asetrate, 92_308)
        self.assertTrue(math.isclose(effective, 96_000 / 92_308))
        direct = operation_configuration(
            "correct-speed-side",
            settings,
            _OBSERVATIONS,
            source_sample_rate=96_000,
            requested_speed_factor=1.04,
            restoration_mode="none",
        )
        reviewed = operation_configuration(
            "correct-speed-side",
            settings,
            _OBSERVATIONS,
            source_sample_rate=96_000,
            requested_speed_factor=1.04,
            restoration_mode="reviewed",
        )
        self.assertEqual(direct["asetrate_hz"], 92_308)
        self.assertEqual(direct["resampler"], "libsoxr")
        self.assertEqual(direct["resampler_precision"], 33)
        self.assertEqual(direct["timeline_origin"], "relative-music-start")
        self.assertNotEqual(direct["input_mode"], reviewed["input_mode"])
        portable = operation_configuration(
            "encode-portable", settings, _OBSERVATIONS
        )
        self.assertEqual(portable["codec"], "aac")
        self.assertEqual(portable["aac_profile"], "aac-lc")
        self.assertEqual(portable["bitrate_bps"], 320_000)
        binding = operation_tool_binding(
            "correct-speed-side",
            settings,
            _OBSERVATIONS,
            source_sample_rate=96_000,
            requested_speed_factor=1.04,
            restoration_mode="none",
        )
        validate_operation_tool_binding(
            "correct-speed-side",
            binding,
            settings,
            _OBSERVATIONS,
            source_sample_rate=96_000,
            requested_speed_factor=1.04,
            restoration_mode="none",
        )
        self.assertEqual(
            binding.configuration["ffmpeg_executable_sha256"], "1" * 64
        )
        with self.assertRaises(ProjectValidationError):
            PublicationSettings(flac_compression=13).validate()
        with self.assertRaises(ProjectValidationError):
            PublicationSettings(aac_bitrate_kbps=True).validate()

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "FFmpeg is required",
    )
    def test_real_ffmpeg_restoration_chain_is_consumed_without_shortcuts(self) -> None:
        sample_rate = 44_100
        frame_count = 35_280
        click_start = 17_000
        time = np.arange(frame_count, dtype=np.float64) / sample_rate
        pcm = np.column_stack(
            (
                0.24 * np.sin(2.0 * np.pi * 233.0 * time + 0.1),
                0.21 * np.sin(2.0 * np.pi * 311.0 * time + 0.2),
            )
        )
        integer_pcm = np.rint(pcm * 32_767.0).astype("<i2")
        integer_pcm[click_start : click_start + 24, 0] = np.iinfo(np.int16).min
        source_path = self.root / "real-side.flac"
        completed = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
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
                "-compression_level",
                "8",
                "-sample_fmt",
                "s16",
                str(source_path),
            ],
            input=integer_pcm.tobytes(),
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        source = probe_audio(source_path, stored_path=source_path.name)
        assert source.sample_count is not None
        project = Project(
            source=source,
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(
                music_start_seconds=0.0,
                music_end_seconds=source.sample_count / source.sample_rate,
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=[
                Track(
                    number=1,
                    title="Real restoration fixture",
                    start_sample=0,
                    end_sample=source.sample_count,
                    start_seconds=0.0,
                    end_seconds=source.sample_count / source.sample_rate,
                )
            ],
        )
        project_path = self.root / "real-side.groove.json"
        save_project(project, project_path)
        album_path = self.root / "real-album.groove-album.json"
        album_side = AlbumSide("A", 1, project_path.name)
        pin_album_side(album_side, album_path)
        save_album_project(
            AlbumProject(
                metadata={"artist": "Fixture", "album": "Real chain"},
                sides=[album_side],
            ),
            album_path,
        )
        workspace = default_restoration_workspace(project_path)
        workspace.mkdir(parents=True)
        scan_path = workspace / f"scan-{'a' * 32}.json"
        scan = scan_project_clicks(project_path, scan_path, max_candidates=100)
        candidates = list(scan["candidates"])
        approved = next(
            item
            for item in candidates
            if item["repairable"] is True
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
        recipe_path = workspace / f"recipe-{'b' * 32}.json"
        create_restoration_recipe(
            project_path,
            scan_path,
            decisions,
            recipe_path,
        )
        render_bundle = workspace / f"render-{'c' * 32}"
        render_restored_side(
            project_path,
            scan_path,
            recipe_path,
            render_bundle,
        )

        plan = build_album_publication_plan(
            album_path,
            self.root / "real-publication.json",
            selected_profiles=(
                PROFILE_RESTORED_SIDE,
                PROFILE_CORRECTED_LOSSLESS,
            ),
            restoration_mode="reviewed",
        )

        binding = plan.sides[0].restoration_render
        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(binding.audio_sha256, sha256_file(render_bundle / "restored.flac"))
        self.assertEqual(
            {node.operation for node in plan.nodes},
            {
                "source-side",
                "restore-side",
                "correct-speed-side",
                "assemble-restored",
                "encode-lossless",
            },
        )

    def test_workspace_name_matches_review_server_policy(self) -> None:
        project = self.root / "Side A (45 RPM)!!.groove.json"
        expected = (
            self.root
            / ".groove-serpent"
            / "restoration"
            / "Side-A-45-RPM-groove"
        )
        self.assertEqual(default_restoration_workspace(project), expected)
        self.assertFalse(expected.exists())

    def test_album_written_by_fixture_remains_valid(self) -> None:
        album_path = self._write_album()
        album = load_album_project(album_path)
        self.assertIsNotNone(album.sides[0].pin)
        self.assertEqual(album.sides[0].project, "side-a.groove.json")


if __name__ == "__main__":
    unittest.main()
