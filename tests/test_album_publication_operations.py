from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from groove_serpent.album import (
    AlbumProject,
    AlbumSide,
    load_album_project_with_sha256,
    pin_album_side,
    save_album_project,
)
from groove_serpent.album_publication_catalog import (
    AlbumPublicationPlanCatalog,
    PublicationPlanCatalogEntry,
)
from groove_serpent.album_publication_durability import (
    AlbumPublicationVerificationReport,
    VerificationMismatch,
)
from groove_serpent.album_publication_executor import _directory_identity, _journal
from groove_serpent.album_publication_operations import (
    discover_album_publication_operations,
)
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.project_io import save_project


class AlbumPublicationOperationCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        source = self.root / "side.flac"
        payload = b"immutable-source" * 8
        source.write_bytes(payload)
        details = source.stat()
        project = Project(
            source=AudioSource(
                path=source.name,
                filename=source.name,
                size_bytes=details.st_size,
                modified_ns=details.st_mtime_ns,
                duration_seconds=1.0,
                sample_rate=1_000,
                channels=2,
                codec_name="flac",
                bits_per_raw_sample=16,
                sample_format="s16",
                sample_count=1_000,
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(
                music_start_seconds=0.0,
                music_end_seconds=1.0,
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=[Track(1, "Track", 0, 1_000, 0.0, 1.0)],
            metadata={"artist": "Artist", "album": "Album"},
        )
        project_path = self.root / "side.groove.json"
        save_project(project, project_path)
        side = AlbumSide("A", 1, project_path.name)
        self.album_path = self.root / "album.groove-album.json"
        pin_album_side(side, self.album_path)
        save_album_project(
            AlbumProject(
                metadata={"artist": "Artist", "album": "Album"},
                sides=[side],
            ),
            self.album_path,
        )
        _album, self.album_sha256 = load_album_project_with_sha256(self.album_path)
        self.plan_sha256 = "a" * 64
        self.plan_file_sha256 = "b" * 64
        self.plan_filename = "album.publication-plan.json"
        self.plan_catalog = AlbumPublicationPlanCatalog(
            album_reference=self.album_path.name,
            album_sha256=self.album_sha256,
            scan_complete=True,
            entries=(
                PublicationPlanCatalogEntry(
                    filename=self.plan_filename,
                    status="current",
                    file_sha256=self.plan_file_sha256,
                    plan_sha256=self.plan_sha256,
                    selected_profiles=("archival-source",),
                    restoration_mode="none",
                    side_count=1,
                    issues=(),
                ),
            ),
            issues=(),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _candidate(self, name: str) -> Path:
        candidate = self.root / name
        candidate.mkdir()
        (candidate / "groove-serpent-album-publication.json").write_text(
            "{}",
            encoding="utf-8",
        )
        (candidate / "groove-serpent-publication-journal.json").write_text(
            "{}",
            encoding="utf-8",
        )
        return candidate

    def _verification(
        self,
        path: Path,
        *,
        ok: bool = True,
    ) -> AlbumPublicationVerificationReport:
        mismatches = ()
        if not ok:
            mismatches = (
                VerificationMismatch(
                    "verification_failed",
                    None,
                    "verified",
                    None,
                    "Synthetic verification failure.",
                ),
            )
        return AlbumPublicationVerificationReport(
            publication_directory=str(path),
            ok=ok,
            manifest_sha256="c" * 64 if ok else None,
            journal_sha256="d" * 64 if ok else None,
            artifact_count=2 if ok else 0,
            mismatches=mismatches,
        )

    def test_current_stale_invalid_classification_is_read_only(self) -> None:
        current = self._candidate("current-publication")
        stale = self._candidate("stale-publication")
        invalid = self._candidate("invalid-publication")
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for root in (current, stale, invalid)
            for path in root.iterdir()
        }

        def verify(path: Path) -> AlbumPublicationVerificationReport:
            return self._verification(path, ok=path.name != invalid.name)

        def manifest(path: Path) -> tuple[dict[str, object], object]:
            album_sha256 = (
                self.album_sha256 if path.parent.name == current.name else "e" * 64
            )
            return (
                {
                    "plan": {
                        "sibling_filename": self.plan_filename,
                        "raw_file_sha256": self.plan_file_sha256,
                        "plan_sha256": self.plan_sha256,
                    },
                    "album": {"sha256": album_sha256},
                },
                SimpleNamespace(sha256="c" * 64),
            )

        def journal(_path: Path) -> tuple[dict[str, str], object, object]:
            return (
                {"plan_sha256": self.plan_sha256},
                SimpleNamespace(sha256="d" * 64),
                object(),
            )

        with mock.patch(
            "groove_serpent.album_publication_operations.verify_album_publication",
            side_effect=verify,
        ), mock.patch(
            "groove_serpent.album_publication_operations.load_album_publication_manifest",
            side_effect=manifest,
        ), mock.patch(
            "groove_serpent.album_publication_operations.load_album_publication_journal",
            side_effect=journal,
        ):
            catalog = discover_album_publication_operations(
                self.album_path,
                self.plan_catalog,
                expected_album_sha256=self.album_sha256,
            )

        self.assertEqual(
            {entry.directory_name: entry.status for entry in catalog.publications},
            {
                current.name: "current",
                stale.name: "stale",
                invalid.name: "invalid",
            },
        )
        for path, receipt in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), receipt)

    def test_directory_swap_between_verification_and_receipts_is_invalid(self) -> None:
        candidate = self._candidate("swapped-publication")
        moved = self.root / "original-publication"

        def manifest(_path: Path) -> tuple[dict[str, object], object]:
            return (
                {
                    "plan": {
                        "sibling_filename": self.plan_filename,
                        "raw_file_sha256": self.plan_file_sha256,
                        "plan_sha256": self.plan_sha256,
                    },
                    "album": {"sha256": self.album_sha256},
                },
                SimpleNamespace(sha256="c" * 64),
            )

        def swap_journal(_path: Path) -> tuple[dict[str, str], object, object]:
            candidate.rename(moved)
            candidate.mkdir()
            (candidate / "groove-serpent-album-publication.json").write_text(
                "{}",
                encoding="utf-8",
            )
            (candidate / "groove-serpent-publication-journal.json").write_text(
                "{}",
                encoding="utf-8",
            )
            return (
                {"plan_sha256": self.plan_sha256},
                SimpleNamespace(sha256="d" * 64),
                object(),
            )

        with mock.patch(
            "groove_serpent.album_publication_operations.verify_album_publication",
            return_value=self._verification(candidate),
        ), mock.patch(
            "groove_serpent.album_publication_operations.load_album_publication_manifest",
            side_effect=manifest,
        ), mock.patch(
            "groove_serpent.album_publication_operations.load_album_publication_journal",
            side_effect=swap_journal,
        ):
            catalog = discover_album_publication_operations(
                self.album_path,
                self.plan_catalog,
            )

        self.assertEqual(len(catalog.publications), 1)
        self.assertEqual(catalog.publications[0].status, "invalid")
        self.assertEqual(
            catalog.publications[0].issues[0].code,
            "receipt_changed_during_discovery",
        )

    def test_reserved_stage_is_only_an_actionable_orphan(self) -> None:
        operation_id = "1" * 32
        stage = self.root / (
            f".groove-serpent-album-publication-{operation_id}.partial"
        )
        stage.mkdir()
        identity = _directory_identity(stage, label="Synthetic publication stage")
        _journal(
            stage,
            "staging",
            self.plan_sha256,
            operation_id=operation_id,
            intended_output_name="new-publication",
            stage_identity=identity,
        )

        catalog = discover_album_publication_operations(
            self.album_path,
            self.plan_catalog,
        )

        self.assertEqual(catalog.publications, ())
        self.assertEqual(len(catalog.orphans), 1)
        orphan = catalog.orphans[0]
        self.assertEqual(orphan.directory_name, stage.name)
        self.assertTrue(orphan.owned)
        self.assertTrue(orphan.belongs_to_album)
        self.assertTrue(orphan.actionable)
        self.assertIsNotNone(orphan.directory_identity)
        assert orphan.directory_identity is not None
        self.assertIsInstance(orphan.directory_identity["inode"], str)


if __name__ == "__main__":
    unittest.main()
