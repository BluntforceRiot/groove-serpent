from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from groove_serpent.album import (
    AlbumProject,
    inspect_album_project,
    parse_album_side_spec,
    save_album_project,
)
from groove_serpent.album_workbench import build_album_workbench_state
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.project_io import save_project


class AlbumWorkbenchTests(unittest.TestCase):
    def _write_project(
        self,
        root: Path,
        stem: str,
        *,
        tracks: int = 1,
        metadata: dict[str, str] | None = None,
    ) -> Path:
        source_path = root / f"{stem}.flac"
        source_payload = (f"immutable-{stem}".encode("utf-8")) * 8
        source_path.write_bytes(source_payload)
        source_stat = source_path.stat()
        sample_rate = 1_000
        track_samples = 1_000
        project_tracks = [
            Track(
                number=index,
                title=f"Track {index}",
                start_sample=(index - 1) * track_samples,
                end_sample=index * track_samples,
                start_seconds=float(index - 1),
                end_seconds=float(index),
            )
            for index in range(1, tracks + 1)
        ]
        project = Project(
            source=AudioSource(
                path=source_path.name,
                filename=source_path.name,
                size_bytes=source_stat.st_size,
                modified_ns=source_stat.st_mtime_ns,
                duration_seconds=float(tracks),
                sample_rate=sample_rate,
                channels=2,
                codec_name="flac",
                bits_per_raw_sample=24,
                sample_format="s32",
                sample_count=tracks * track_samples,
                sha256=hashlib.sha256(source_payload).hexdigest(),
            ),
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(
                music_start_seconds=0.0,
                music_end_seconds=float(tracks),
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=project_tracks,
            metadata=dict(metadata or {}),
        )
        project_path = root / f"{stem}.groove.json"
        save_project(project, project_path)
        return project_path

    def _write_album(
        self,
        root: Path,
        projects: list[Path],
        *,
        metadata: dict[str, str] | None = None,
        override_first_side: bool = False,
    ) -> tuple[AlbumProject, Path]:
        album_path = root / "record.groove-album.json"
        sides = []
        for order, project_path in enumerate(projects, start=1):
            label = chr(ord("A") + order - 1)
            spec = f"{label}|{project_path}"
            if order == 1 and override_first_side:
                spec += "|34.0|33.333333333|1.0"
            sides.append(parse_album_side_spec(spec, order, album_path))
        album = AlbumProject(
            metadata=dict(metadata or {}),
            sides=sides,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        save_album_project(album, album_path)
        return album, album_path

    def test_state_has_stable_order_ids_counts_and_exact_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            album_metadata = {
                "artist": "Album Artist",
                "album": "Album Title",
                "album_artist": "Album Artist",
                "year": "2026",
                "genre": "Metal",
            }
            side_metadata = {
                "artist": "Different Artist",
                "album": "Different Album",
                "album_artist": "Different Album Artist",
                "year": "2025",
                "genre": "Jazz",
            }
            side_a = self._write_project(
                root, "side-a", tracks=2, metadata=side_metadata
            )
            side_b = self._write_project(root, "side-b", metadata=side_metadata)
            album, album_path = self._write_album(
                root,
                [side_a, side_b],
                metadata=album_metadata,
                override_first_side=True,
            )

            first = build_album_workbench_state(album, album_path)
            second = build_album_workbench_state(album, album_path)

            self.assertEqual(first, second)
            self.assertEqual(first["schema"], "groove-serpent.album-workbench/4")
            self.assertEqual(
                first["identification"]["catalog"]["schema"],
                "groove-serpent.album-identification-catalog/1",
            )
            self.assertEqual(
                first["identification"]["catalog"]["summary"],
                {
                    "total": 0,
                    "current": 0,
                    "stale": 0,
                    "invalid": 0,
                    "selectable": 0,
                },
            )
            self.assertEqual(
                first["identification"]["readiness"],
                {
                    "can_scan": False,
                    "reason_codes": ["recognition_provider_not_ready"],
                },
            )
            self.assertFalse(
                first["identification"]["authority"]["automatic_network_requests"]
            )
            self.assertFalse(
                first["identification"]["authority"]["automatic_metadata_application"]
            )
            self.assertFalse(
                first["identification"]["authority"][
                    "automatic_artwork_download_or_application"
                ]
            )
            self.assertFalse(
                first["identification"]["authority"]["physical_pressing_proven"]
            )
            self.assertEqual(
                first["publication"]["catalog"]["schema"],
                "groove-serpent.album-publication-plan-catalog/1",
            )
            self.assertTrue(
                first["publication"]["readiness"]["can_create_plan"]
            )
            self.assertEqual(
                first["publication"]["operations"]["schema"],
                "groove-serpent.album-publication-operation-catalog/1",
            )
            self.assertTrue(first["publication"]["operations"]["scan_complete"])
            self.assertEqual(
                first["publication"]["operations"]["summary"]["publications"],
                0,
            )
            self.assertFalse(
                first["publication"]["authority"]["automatic_plan_creation"]
            )
            self.assertFalse(
                first["publication"]["authority"]["automatic_execution"]
            )
            self.assertTrue(
                first["publication"]["authority"]["owner_confirmation_required"]
            )
            self.assertFalse(first["publication"]["authority"]["resume_available"])
            self.assertEqual(first["album_revision"], album.revision)
            self.assertEqual(
                first["side_order_policy"],
                {
                    "approval_relevant": True,
                    "reorder_invalidates_all_side_pins": True,
                    "reason": (
                        "Side order determines continuous album numbering and "
                        "publication order, so every reordered side must be reviewed "
                        "and repinned."
                    ),
                },
            )
            self.assertEqual(first["total_tracks"], 3)
            self.assertEqual(first["total_sides"], 2)
            self.assertTrue(first["ready_for_export"])
            self.assertEqual(
                first["summary"],
                {
                    "total": 11,
                    "blockers": 0,
                    "reviews": 11,
                    "sides_ready": 2,
                    "sides_blocked": 0,
                },
            )
            expected_ids = [
                "side:001:speed-override-differs-from-project",
                "side:001:metadata:artist",
                "side:001:metadata:album",
                "side:001:metadata:album-artist",
                "side:001:metadata:year",
                "side:001:metadata:genre",
                "side:002:metadata:artist",
                "side:002:metadata:album",
                "side:002:metadata:album-artist",
                "side:002:metadata:year",
                "side:002:metadata:genre",
            ]
            self.assertEqual(
                [item["id"] for item in first["exceptions"]], expected_ids
            )
            self.assertEqual(
                len(expected_ids), len(set(item["id"] for item in first["exceptions"]))
            )
            current = first["sides"][0]["current_identity"]
            self.assertEqual(
                set(current),
                {
                    "project_revision",
                    "project_sha256",
                    "editable_state_sha256",
                    "source_sha256",
                    "project_speed_state_sha256",
                },
            )
            self.assertIsNotNone(first["sides"][0]["pin"])
            self.assertNotIn("current", first["sides"][0])

    def test_each_inspection_drift_reason_becomes_a_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_path = self._write_project(root, "side")
            album, album_path = self._write_album(
                root,
                [project_path],
                metadata={"artist": "Artist", "album": "Title"},
            )
            inspected = inspect_album_project(album, album_path)
            reasons = [
                "side is unpinned",
                "project revision changed",
                "project file changed",
                "editable project state changed",
                "source audio changed",
                "reviewed project speed state changed",
                "album speed selection changed",
                "source no longer matches the side project",
            ]
            inspected["ready_for_export"] = False
            inspected["sides"][0]["ready_for_export"] = False
            inspected["sides"][0]["drift"] = reasons
            inspected["sides"][0]["pinned"] = False
            inspected["sides"][0]["pin"] = None

            with mock.patch(
                "groove_serpent.album_workbench.inspect_album_project",
                return_value=copy.deepcopy(inspected),
            ):
                state = build_album_workbench_state(album, album_path)

            self.assertEqual(
                [item["type"] for item in state["exceptions"]],
                [
                    "side_unpinned",
                    "project_revision_changed",
                    "project_file_changed",
                    "editable_project_state_changed",
                    "source_audio_changed",
                    "reviewed_project_speed_state_changed",
                    "album_speed_selection_changed",
                    "source_project_mismatch",
                ],
            )
            self.assertEqual(state["summary"]["blockers"], 8)
            self.assertEqual(state["summary"]["reviews"], 0)
            self.assertFalse(state["ready_for_export"])
            self.assertTrue(
                all(item["severity"] == "blocker" for item in state["exceptions"])
            )
            self.assertEqual(
                state["exceptions"][1]["evidence"],
                {
                    "pinned": None,
                    "current": state["sides"][0]["current_identity"][
                        "project_revision"
                    ],
                },
            )

    def test_metadata_normalization_and_album_alias_avoid_false_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_path = self._write_project(
                root,
                "side",
                metadata={
                    "artist": "Cafe\u0301 Artist",
                    "title": "the album",
                    "album_artist": "Cafe\u0301 Artist",
                    "year": "2026",
                    "genre": "death metal",
                },
            )
            album, album_path = self._write_album(
                root,
                [project_path],
                metadata={
                    "artist": " CAFÉ   ARTIST ",
                    "album": " THE   ALBUM ",
                    "album_artist": "CAFÉ ARTIST",
                    "year": " 2026 ",
                    "genre": "DEATH   METAL",
                },
            )

            state = build_album_workbench_state(album, album_path)

            self.assertEqual(state["exceptions"], [])
            self.assertTrue(state["ready_for_export"])

    def test_explicit_speed_override_is_review_only_with_exact_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_path = self._write_project(
                root,
                "side",
                metadata={"artist": "Artist", "album": "Title"},
            )
            album, album_path = self._write_album(
                root,
                [project_path],
                metadata={"artist": "Artist", "album": "Title"},
                override_first_side=True,
            )

            state = build_album_workbench_state(album, album_path)

            self.assertEqual(len(state["exceptions"]), 1)
            exception = state["exceptions"][0]
            self.assertEqual(exception["type"], "speed_override_differs_from_project")
            self.assertEqual(exception["severity"], "review")
            self.assertNotEqual(
                exception["evidence"]["selected_speed_state_sha256"],
                exception["evidence"]["project_speed_state_sha256"],
            )
            self.assertEqual(
                exception["actions"], ["review_speed", "inherit_project_speed"]
            )
            self.assertTrue(state["ready_for_export"])

    def test_missing_essential_album_metadata_is_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_path = self._write_project(
                root,
                "side",
                metadata={"artist": "Side Artist", "album": "Side Title"},
            )
            album, album_path = self._write_album(
                root,
                [project_path],
                metadata={"artist": "   ", "title": "\t"},
            )

            state = build_album_workbench_state(album, album_path)

            self.assertEqual(
                [item["id"] for item in state["exceptions"]],
                ["album:missing-album-artist", "album:missing-album-title"],
            )
            self.assertEqual(
                [item["field"] for item in state["exceptions"]],
                ["album_artist", "album_title"],
            )
            self.assertEqual(state["summary"]["blockers"], 2)
            self.assertFalse(state["ready_for_export"])

    def test_album_side_metadata_conflicts_have_deterministic_exact_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_path = self._write_project(
                root,
                "side",
                metadata={
                    "artist": "Side Artist",
                    "album": "Side Title",
                    "album_artist": "Side Album Artist",
                    "year": "2025",
                    "genre": "Jazz",
                },
            )
            album, album_path = self._write_album(
                root,
                [project_path],
                metadata={
                    "artist": "Album Artist",
                    "album": "Album Title",
                    "album_artist": "Album Album Artist",
                    "year": "2026",
                    "genre": "Metal",
                },
            )

            state = build_album_workbench_state(album, album_path)

            conflicts = state["exceptions"]
            self.assertEqual(
                [item["field"] for item in conflicts],
                ["artist", "album", "album_artist", "year", "genre"],
            )
            self.assertTrue(
                all(item["type"] == "album_side_metadata_conflict" for item in conflicts)
            )
            self.assertEqual(
                conflicts[0]["evidence"],
                {"album": "Album Artist", "side_project": "Side Artist"},
            )
            self.assertTrue(state["ready_for_export"])

    def test_builder_does_not_mutate_album_or_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_path = self._write_project(
                root,
                "side",
                metadata={"artist": "Artist", "album": "Title"},
            )
            album, album_path = self._write_album(
                root,
                [project_path],
                metadata={"artist": "Artist", "album": "Title"},
            )
            source_path = root / "side.flac"
            before_album = asdict(album)
            before_files = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (album_path, project_path, source_path)
            }

            build_album_workbench_state(album, album_path)

            self.assertEqual(asdict(album), before_album)
            for path, receipt in before_files.items():
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), receipt)


if __name__ == "__main__":
    unittest.main()
