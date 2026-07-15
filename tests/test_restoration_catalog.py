from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from groove_serpent import __version__
from groove_serpent.media import sha256_file
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.project_io import load_project, save_project
from groove_serpent.restoration import MAX_REPAIR_SAMPLES
from groove_serpent.restoration_catalog import discover_restoration_catalog
from groove_serpent.restoration_workflow import (
    PREVIEW_SCHEMA,
    RECIPE_SCHEMA,
    REMOVED_SIGNAL_GAIN,
    RENDER_SCHEMA,
    REPAIR_BACKEND,
    SCAN_SCHEMA,
    _candidate_identifier,
    _detector_manifest,
)


class RestorationCatalogTests(unittest.TestCase):
    scan_name = f"scan-{'1' * 32}.json"
    recipe_name = f"recipe-{'2' * 32}.json"
    preview_name = f"preview-{'3' * 32}"
    render_name = f"render-{'4' * 32}"

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source_path = self.root / "side.flac"
        self.source_path.write_bytes(bytes(range(256)) * 16)
        metadata = self.source_path.stat()
        source = AudioSource(
            path=self.source_path.name,
            filename=self.source_path.name,
            size_bytes=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
            duration_seconds=10.0,
            sample_rate=1_000,
            channels=2,
            codec_name="flac",
            bits_per_raw_sample=16,
            sample_format="s16",
            sample_count=10_000,
            sha256=sha256_file(self.source_path),
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
                    title="Track one",
                    start_sample=1_000,
                    end_sample=9_000,
                    start_seconds=1.0,
                    end_seconds=9.0,
                )
            ],
        )
        self.project_path = self.root / "side.groove.json"
        save_project(project, self.project_path)
        self.workspace = self.root / ".groove-serpent" / "restoration" / "side"
        self.workspace.mkdir(parents=True)
        self.paths = self._write_full_chain()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _coverage(self) -> dict[str, Any]:
        return {
            "music_start_frame": 1_000,
            "music_end_frame_exclusive": 9_000,
            "music_frame_count": 8_000,
            "scanned_music_frames": 8_000,
            "scanned_music_percent": 100.0,
            "scan_range_covers_music": True,
            "candidate_scan_truncated": False,
            "detected_candidates": 1,
            "retained_candidates": 1,
            "unretained_detections": 0,
            "unreviewed_regions": [],
            "restoration_status": "complete",
        }

    def _candidate(self, source_sha256: str) -> dict[str, Any]:
        candidate_id = _candidate_identifier(
            source_sha256=source_sha256,
            kind="impulse",
            start_frame=2_000,
            end_frame=2_004,
            peak_frame=2_001,
            channels=(0,),
        )
        return {
            "id": candidate_id,
            "type": "impulse",
            "detected_start_frame": 2_000,
            "detected_end_frame_exclusive": 2_004,
            "start_frame": 2_000,
            "end_frame_exclusive": 2_004,
            "peak_frame": 2_001,
            "channels": [0],
            "confidence": 0.8,
            "repairable": True,
            "start_seconds": 2.0,
            "end_seconds": 2.004,
        }

    def _write_full_chain(self) -> dict[str, Path]:
        source_sha = sha256_file(self.source_path)
        project_sha = sha256_file(self.project_path)
        candidate = self._candidate(source_sha)
        coverage = self._coverage()
        scan_path = self.workspace / self.scan_name
        scan = {
            "schema": SCAN_SCHEMA,
            "created_at": "2026-07-13T00:00:00Z",
            "app_version": __version__,
            "project": {"path": self.project_path.name, "sha256": project_sha},
            "source": {
                "path": self.source_path.name,
                "sha256": source_sha,
                "size_bytes": self.source_path.stat().st_size,
                "sample_rate": 1_000,
                "channels": 2,
                "bits_per_raw_sample": 16,
                "sample_count": 10_000,
            },
            "decoder": {
                "ffmpeg": "ffmpeg test",
                "canonical_pcm": "s16le-interleaved",
                "bytes_per_frame": 4,
                "immutable_source_snapshot": True,
                "source_snapshot_sha256": source_sha,
            },
            "detector": _detector_manifest(),
            "scan": {
                "start_frame": 1_000,
                "end_frame_exclusive": 9_000,
                "start_seconds": 1.0,
                "end_seconds": 9.0,
            },
            "candidates": [candidate],
            "summary": {
                "detected": 1,
                "retained": 1,
                "truncated": False,
                "clipped": 0,
                "impulse": 1,
                "repairable": 1,
            },
            "coverage": coverage,
        }
        self._write_json(scan_path, scan)
        scan_sha = self._sha(scan_path)

        recipe_path = self.workspace / self.recipe_name
        recipe = {
            "schema": RECIPE_SCHEMA,
            "created_at": "2026-07-13T00:01:00Z",
            "app_version": __version__,
            "project": {"path": self.project_path.name, "sha256": project_sha},
            "source": {"path": self.source_path.name, "sha256": source_sha},
            "scan": {"path": scan_path.name, "sha256": scan_sha},
            "backend": {
                "name": REPAIR_BACKEND,
                "maximum_repair_frames": MAX_REPAIR_SAMPLES,
            },
            "decisions": [{"candidate_id": candidate["id"], "decision": "approved"}],
            "summary": {
                "candidates": 1,
                "approved": 1,
                "rejected": 0,
                "protected": 0,
            },
            "coverage": coverage,
        }
        self._write_json(recipe_path, recipe)
        recipe_sha = self._sha(recipe_path)

        preview_bundle = self.workspace / self.preview_name
        preview_bundle.mkdir()
        preview_files: dict[str, dict[str, str]] = {}
        for role, content in {
            "before": b"BEFORE-FLAC",
            "proposed": b"PROPOSED-FLAC",
            "removed": b"REMOVED-FLAC",
        }.items():
            output = preview_bundle / f"{role}.flac"
            output.write_bytes(content)
            preview_files[role] = {
                "path": output.name,
                "sha256": self._sha(output),
            }
        boundary = {
            "candidate_id": candidate["id"],
            "channels": [0],
            "left_jump": [10],
            "right_jump": [11],
        }
        preview = {
            "schema": PREVIEW_SCHEMA,
            "created_at": "2026-07-13T00:02:00Z",
            "app_version": __version__,
            "source": {
                "path": self.source_path.name,
                "sha256": source_sha,
                "sample_rate": 1_000,
                "channels": 2,
                "bits_per_raw_sample": 16,
            },
            "scan": {"path": scan_path.name, "sha256": scan_sha},
            "candidates": [candidate],
            "context": {
                "start_frame": 1_900,
                "end_frame_exclusive": 2_100,
                "repair_start_in_preview": 100,
                "repair_end_in_preview_exclusive": 104,
                "repair_windows": [
                    {
                        "candidate_id": candidate["id"],
                        "start_in_preview": 100,
                        "end_in_preview_exclusive": 104,
                        "channels": [0],
                    }
                ],
            },
            "backend": {
                "name": REPAIR_BACKEND,
                "maximum_repair_frames": MAX_REPAIR_SAMPLES,
                "audacity_used": False,
                "immutable_source_snapshot": True,
                "source_snapshot_sha256": source_sha,
            },
            "files": preview_files,
            "audition": {
                "before_linear_gain": 1.0,
                "proposed_linear_gain": 1.0,
                "removed_linear_gain": REMOVED_SIGNAL_GAIN,
                "removed_gain_db": 20.0 * math.log10(REMOVED_SIGNAL_GAIN),
                "definition": ("removed = (before - proposed) * removed_linear_gain"),
                "matched_original_level": True,
            },
            "metrics": {
                "before": {
                    "approved_peak_absolute_sample": 1_000,
                    "approved_local_curvature_rms": 12.5,
                    "window_boundaries": [boundary],
                },
                "proposed": {
                    "approved_peak_absolute_sample": 900,
                    "approved_local_curvature_rms": 9.5,
                    "window_boundaries": [boundary],
                },
                "changed_scalar_samples": 4,
                "removed_peak_absolute_sample": 100,
                "removed_clipped_scalar_samples": 0,
            },
            "proof": {
                "source_unchanged": True,
                "immutable_source_snapshot": True,
                "lossless_preview_round_trip": True,
                "outside_approved_windows_and_channels_identical": True,
                "frame_count_equal": True,
                "format_equal": True,
                "removed_signal_matches_declared_difference": True,
            },
            "approval": {
                "status": "pending",
                "instruction": "Audition all three lossless previews.",
            },
        }
        self._write_json(preview_bundle / "preview.json", preview)

        render_bundle = self.workspace / self.render_name
        render_bundle.mkdir()
        restored = render_bundle / "restored.flac"
        restored.write_bytes(b"RESTORED-FLAC")
        render = {
            "schema": RENDER_SCHEMA,
            "created_at": "2026-07-13T00:03:00Z",
            "app_version": __version__,
            "project": {"path": self.project_path.name, "sha256": project_sha},
            "source": {"path": self.source_path.name, "sha256": source_sha},
            "scan": {"path": scan_path.name, "sha256": scan_sha},
            "recipe": {
                "path": recipe_path.name,
                "sha256": recipe_sha,
                "schema": RECIPE_SCHEMA,
            },
            "music_range": {
                "start_frame": 1_000,
                "end_frame_exclusive": 9_000,
                "sample_count": 8_000,
            },
            "coverage": coverage,
            "backend": {
                "name": REPAIR_BACKEND,
                "maximum_repair_frames": MAX_REPAIR_SAMPLES,
                "streaming_source_decode": True,
                "audacity_used": False,
                "immutable_source_snapshot": True,
                "source_snapshot_sha256": source_sha,
            },
            "repairs": [
                {
                    "candidate_id": candidate["id"],
                    "start_frame": 2_000,
                    "end_frame_exclusive": 2_004,
                    "channels": [0],
                    "source_pcm_sha256": "a" * 64,
                    "restored_pcm_sha256": "b" * 64,
                    "changed_scalar_samples": 4,
                }
            ],
            "protected": [],
            "files": {
                "restored": {
                    "path": restored.name,
                    "sha256": self._sha(restored),
                    "sample_count": 8_000,
                    "sample_rate": 1_000,
                    "channels": 2,
                    "bits_per_raw_sample": 16,
                }
            },
            "pcm_proof": {
                "source_music_range_sha256": "c" * 64,
                "restored_music_range_sha256": "d" * 64,
                "outside_approved_windows_and_channels_identical": True,
                "approved_patches_match_receipt_hashes": True,
            },
            "proof": {
                "source_unchanged": True,
                "immutable_source_snapshot": True,
                "project_unchanged": True,
                "scan_unchanged": True,
                "recipe_unchanged": True,
                "lossless_flac_round_trip": True,
                "frame_count_equal_to_project_music_range": True,
                "format_equal_to_source": True,
            },
        }
        self._write_json(render_bundle / "render.json", render)
        return {
            "scan": scan_path,
            "recipe": recipe_path,
            "preview": preview_bundle / "preview.json",
            "render": render_bundle / "render.json",
            "proposed": preview_bundle / "proposed.flac",
        }

    def _tree_snapshot(self) -> dict[str, tuple[str, int, str]]:
        result: dict[str, tuple[str, int, str]] = {}
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix()
            if path.is_symlink():
                result[relative] = ("link", 0, os.readlink(path))
            elif path.is_file():
                metadata = path.stat()
                result[relative] = (
                    "file",
                    metadata.st_mtime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            else:
                result[relative] = ("directory", path.stat().st_mtime_ns, "")
        return result

    def test_valid_full_chain_restart_order_ids_and_no_writes(self) -> None:
        before = self._tree_snapshot()
        first = discover_restoration_catalog(self.workspace, self.project_path)
        second = discover_restoration_catalog(self.workspace, self.project_path)
        after = self._tree_snapshot()

        self.assertEqual(before, after)
        self.assertEqual(first.invalid, ())
        self.assertEqual(first.stale, ())
        self.assertEqual(
            [artifact.kind for artifact in first.artifacts],
            ["scan", "recipe", "preview", "render"],
        )
        self.assertEqual(
            [artifact.artifact_id for artifact in first.artifacts],
            [artifact.artifact_id for artifact in second.artifacts],
        )
        self.assertTrue(
            all(
                artifact.artifact_id == f"{artifact.kind}-{artifact.manifest_sha256[:32]}"
                for artifact in first.artifacts
            )
        )
        for kind in ("scan", "recipe", "preview", "render"):
            latest = first.latest(kind)
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest.kind, kind)
        selection = first.latest_chain()
        self.assertEqual(
            [
                selection.scan.kind if selection.scan else None,
                selection.recipe.kind if selection.recipe else None,
                selection.preview.kind if selection.preview else None,
                selection.render.kind if selection.render else None,
            ],
            ["scan", "recipe", "preview", "render"],
        )
        self.assertEqual(len(first.latest("preview").files), 3)  # type: ignore[union-attr]
        self.assertEqual(len(first.latest("render").files), 1)  # type: ignore[union-attr]

    def test_tampered_referenced_bytes_are_invalid_not_stale(self) -> None:
        self.paths["proposed"].write_bytes(b"TAMPERED")
        catalog = discover_restoration_catalog(self.workspace, self.project_path)
        self.assertEqual([item.kind for item in catalog.artifacts], ["scan", "recipe", "render"])
        self.assertEqual(catalog.stale, ())
        self.assertIn("output_hash_mismatch", {issue.code for issue in catalog.invalid})

    def test_dependency_name_and_hash_mismatch_rejects_chain(self) -> None:
        recipe = json.loads(self.paths["recipe"].read_text(encoding="utf-8"))
        recipe["scan"]["sha256"] = "0" * 64
        self._write_json(self.paths["recipe"], recipe)

        catalog = discover_restoration_catalog(self.workspace, self.project_path)
        self.assertEqual([item.kind for item in catalog.artifacts], ["scan", "preview"])
        self.assertEqual(catalog.stale, ())
        codes = {issue.code for issue in catalog.invalid}
        self.assertIn("dependency_hash_mismatch", codes)
        self.assertTrue(codes & {"invalid_dependency", "dependency_hash_mismatch"})

    def test_old_project_and_source_chain_is_reported_stale(self) -> None:
        self.source_path.write_bytes(self.source_path.read_bytes() + b"NEW")
        old = load_project(self.project_path)
        metadata = self.source_path.stat()
        current_source = AudioSource(
            path=self.source_path.name,
            filename=self.source_path.name,
            size_bytes=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
            duration_seconds=old.source.duration_seconds,
            sample_rate=old.source.sample_rate,
            channels=old.source.channels,
            codec_name=old.source.codec_name,
            bits_per_raw_sample=old.source.bits_per_raw_sample,
            sample_format=old.source.sample_format,
            sample_count=old.source.sample_count,
            sha256=sha256_file(self.source_path),
        )
        current = Project(
            source=current_source,
            settings=old.settings,
            analysis=old.analysis,
            tracks=old.tracks,
            metadata={**old.metadata, "capture": "replaced"},
        )
        save_project(current, self.project_path)

        catalog = discover_restoration_catalog(self.workspace, self.project_path)
        self.assertEqual(catalog.artifacts, ())
        self.assertEqual(catalog.invalid, ())
        self.assertEqual(
            [artifact.kind for artifact in catalog.stale],
            ["scan", "recipe", "preview", "render"],
        )
        scan = next(item for item in catalog.stale if item.kind == "scan")
        self.assertEqual(
            set(scan.stale_reasons),
            {"project_identity_changed", "source_identity_changed"},
        )
        preview = next(item for item in catalog.stale if item.kind == "preview")
        self.assertIn("stale_scan_dependency", preview.stale_reasons)

    def test_symlink_escape_is_recorded_while_safe_chain_survives(self) -> None:
        outside = self.root / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        escaped = self.workspace / f"scan-{'9' * 32}.json"
        try:
            escaped.symlink_to(outside)
        except OSError:
            # Windows may require Developer Mode for a real symlink.  Exercise
            # the same lstat reparse-point branch without weakening the test.
            escaped.write_text("{}", encoding="utf-8")
            real_lstat = Path.lstat

            def reparse_lstat(path: Path) -> Any:
                metadata = real_lstat(path)
                if path == escaped:
                    values = {
                        name: getattr(metadata, name)
                        for name in (
                            "st_mode",
                            "st_dev",
                            "st_ino",
                            "st_size",
                            "st_mtime_ns",
                            "st_ctime_ns",
                        )
                    }
                    return SimpleNamespace(**values, st_file_attributes=0x400)
                return metadata

            with patch(
                "groove_serpent.restoration_catalog.Path.lstat",
                new=reparse_lstat,
            ):
                catalog = discover_restoration_catalog(self.workspace, self.project_path)
        else:
            catalog = discover_restoration_catalog(self.workspace, self.project_path)
        self.assertEqual(len(catalog.artifacts), 4)
        issue = next(item for item in catalog.invalid if item.path == escaped)
        self.assertEqual(issue.code, "unsafe_reparse_path")

    def test_manifest_path_escape_is_rejected(self) -> None:
        preview = json.loads(self.paths["preview"].read_text(encoding="utf-8"))
        preview["files"]["before"]["path"] = "../outside.flac"
        self._write_json(self.paths["preview"], preview)

        catalog = discover_restoration_catalog(self.workspace, self.project_path)
        self.assertEqual(
            [artifact.kind for artifact in catalog.artifacts],
            ["scan", "recipe", "render"],
        )
        self.assertIn("unsafe_manifest_path", {issue.code for issue in catalog.invalid})

    def test_duplicate_keys_nan_and_corrupt_json_are_rejected(self) -> None:
        duplicate = self.workspace / f"scan-{'7' * 32}.json"
        duplicate.write_text(
            '{"schema":"groove-serpent.click-scan/1","schema":"groove-serpent.click-scan/1"}',
            encoding="utf-8",
        )
        nan_recipe = self.workspace / f"recipe-{'8' * 32}.json"
        nan_recipe.write_text(
            '{"schema":"groove-serpent.restoration-recipe/1","value":NaN}',
            encoding="utf-8",
        )
        corrupt_bundle = self.workspace / f"preview-{'6' * 32}"
        corrupt_bundle.mkdir()
        (corrupt_bundle / "preview.json").write_bytes(b"\xffnot-json")

        catalog = discover_restoration_catalog(self.workspace, self.project_path)
        self.assertEqual(len(catalog.artifacts), 4)
        invalid_paths = {issue.path for issue in catalog.invalid}
        self.assertEqual(
            invalid_paths,
            {duplicate, nan_recipe, corrupt_bundle / "preview.json"},
        )
        self.assertEqual({issue.code for issue in catalog.invalid}, {"invalid_json"})


if __name__ == "__main__":
    unittest.main()
