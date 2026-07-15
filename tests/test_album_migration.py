from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import groove_serpent.album as album_module
import groove_serpent.album_migration as migration_module
from groove_serpent.album import (
    ALBUM_SCHEMA,
    AlbumProject,
    AlbumSide,
    load_album_project,
    resolve_album_reference,
    save_album_project,
)
from groove_serpent.atomic_create import rename_no_replace
from groove_serpent.album_migration import (
    ALBUM_MIGRATION_RECEIPT_SCHEMA,
    album_migration_artifact_paths,
    migrate_album_data,
    migrate_album_file,
)
from groove_serpent.cli import main
from groove_serpent.errors import ProjectValidationError
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.project_io import decode_project_json, load_project, save_project


FIXTURES = Path(__file__).parent / "fixtures" / "album_migrations"
PROJECT_FIXTURES = Path(__file__).parent / "fixtures" / "project_migrations"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _current_side_project() -> Project:
    return Project(
        source=AudioSource(
            path="side.flac",
            filename="side.flac",
            size_bytes=1234,
            modified_ns=123,
            duration_seconds=10.0,
            sample_rate=1000,
            channels=2,
            codec_name="flac",
            bits_per_raw_sample=24,
            sample_format="s32",
            sample_count=10000,
            sha256="a" * 64,
        ),
        settings=AnalysisSettings(min_track_seconds=1.0),
        analysis=AnalysisSummary(
            music_start_seconds=0.0,
            music_end_seconds=10.0,
            noise_floor_db=-50.0,
            silence_threshold_db=-44.0,
            active_threshold_db=-32.0,
            envelope_window_seconds=0.05,
        ),
        tracks=[Track(1, "One", 0, 10000, 0.0, 10.0)],
        metadata={"artist": "Fixture Artist", "album": "Fixture Album"},
    )


class AlbumMigrationTests(unittest.TestCase):
    def _copy_fixture(self, directory: Path, schema: int) -> tuple[Path, bytes]:
        raw = (FIXTURES / f"schema-{schema}.json").read_bytes()
        path = directory / f"album-{schema}.json"
        path.write_bytes(raw)
        return path, raw

    def _write_current_side(self, directory: Path) -> Path:
        path = directory / "side.groove.json"
        save_project(_current_side_project(), path)
        return path

    def test_golden_v1_v2_migrate_exactly_without_pin_changes(self) -> None:
        for schema in (1, 2):
            with self.subTest(schema=schema), tempfile.TemporaryDirectory() as value:
                directory = Path(value)
                side_path = self._write_current_side(directory)
                side_before = side_path.read_bytes()
                path, original = self._copy_fixture(directory, schema)
                original_data = decode_project_json(original)
                before_entries = tuple(sorted(item.name for item in directory.iterdir()))
                with self.assertRaisesRegex(
                    ProjectValidationError, "album migrate ALBUM"
                ):
                    load_album_project(path)
                self.assertEqual(
                    tuple(sorted(item.name for item in directory.iterdir())),
                    before_entries,
                )
                self.assertEqual(path.read_bytes(), original)

                result = migrate_album_file(path)
                self.assertEqual(result.status, "migrated")
                self.assertEqual(result.original_sha256, _sha256(original))
                backup = directory / str(result.backup)
                receipt = directory / str(result.receipt)
                self.assertEqual(backup.read_bytes(), original)
                self.assertEqual(result.migrated_sha256, _sha256(path.read_bytes()))
                self.assertEqual(side_path.read_bytes(), side_before)

                migrated = load_album_project(path)
                self.assertEqual(migrated.schema, ALBUM_SCHEMA)
                self.assertEqual(migrated.revision, 1)
                self.assertEqual(migrated.created_at, original_data["created_at"])
                self.assertEqual(migrated.updated_at, original_data["updated_at"])
                self.assertEqual(migrated.metadata, original_data["metadata"])
                self.assertEqual(migrated.to_dict()["artwork"], original_data["artwork"])
                if schema == 1:
                    self.assertIsNone(migrated.sides[0].pin)
                    self.assertEqual(migrated.sides[0].speed.mode, "override")
                    self.assertEqual(migrated.sides[0].capture_rpm, 45.0)
                    self.assertIsNone(
                        migrated.sides[0].speed.project_speed_state_sha256
                    )
                else:
                    self.assertEqual(
                        migrated.to_dict()["sides"], original_data["sides"]
                    )

                receipt_data = decode_project_json(receipt.read_bytes())
                self.assertEqual(
                    receipt_data["schema"], ALBUM_MIGRATION_RECEIPT_SCHEMA
                )
                plan = receipt_data["plan"]
                self.assertEqual(plan["original_sha256"], _sha256(original))
                self.assertEqual(plan["migrated_sha256"], _sha256(path.read_bytes()))
                self.assertEqual(plan["side_projects"][0]["project"], "side.groove.json")
                self.assertNotIn(str(directory), receipt.read_text(encoding="utf-8"))
                if schema == 1:
                    self.assertIn("pin remains null", plan["steps"][0]["effect"])

    def test_long_valid_album_filename_uses_bounded_private_names(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            self._write_current_side(directory)
            original = (FIXTURES / "schema-1.json").read_bytes()
            path = directory / ("a" * 205 + ".json")
            path.write_bytes(original)

            result = migrate_album_file(path)

            self.assertEqual(result.status, "migrated")
            self.assertEqual(load_album_project(path).schema, ALBUM_SCHEMA)
            self.assertTrue(all(len(item.name) <= 255 for item in directory.iterdir()))
            self.assertFalse(list(directory.glob("*.tmp")))
            self.assertFalse(list(directory.glob("*.preserved")))

    def test_current_repeat_is_zero_write_and_does_not_read_side_project(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            self._write_current_side(directory)
            path, _ = self._copy_fixture(directory, 2)
            migrate_album_file(path)
            before = {
                item.name: (item.read_bytes(), item.stat().st_mtime_ns)
                for item in directory.iterdir()
            }
            with patch.object(
                migration_module,
                "load_project_with_sha256",
                side_effect=AssertionError("current migration read a side"),
            ):
                result = migrate_album_file(path)
            after = {
                item.name: (item.read_bytes(), item.stat().st_mtime_ns)
                for item in directory.iterdir()
            }
        self.assertEqual(result.status, "current")
        self.assertEqual(before, after)

    def test_new_save_preserves_unpinned_state_and_overwrite_increments_revision(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path = directory / "unpinned.album.json"
            album = AlbumProject({}, [AlbumSide("A", 1, "missing.groove.json")])
            with patch.object(
                album_module,
                "pin_album_side",
                side_effect=AssertionError("save attempted to pin"),
            ), patch.object(
                album_module,
                "load_project_with_sha256",
                side_effect=AssertionError("save attempted to read side"),
            ):
                save_album_project(album, path)
                self.assertEqual(album.revision, 1)
                album.metadata["note"] = "explicitly still unpinned"
                save_album_project(album, path, overwrite=True)
            loaded = load_album_project(path)
        self.assertEqual(loaded.revision, 2)
        self.assertIsNone(loaded.sides[0].pin)
        self.assertEqual(loaded.metadata["note"], "explicitly still unpinned")

    def test_new_save_never_overwrites_destination_that_races_into_existence(self) -> None:
        real_create = rename_no_replace
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path = directory / "race.album.json"
            album = AlbumProject({}, [AlbumSide("A", 1, "side.groove.json")])

            def raced_create(source: Path, destination: Path) -> None:
                Path(destination).write_bytes(b"external winner")
                real_create(source, destination)

            with patch.object(
                album_module, "rename_no_replace", side_effect=raced_create
            ):
                with self.assertRaisesRegex(ProjectValidationError, "appeared"):
                    save_album_project(album, path)
            self.assertEqual(path.read_bytes(), b"external winner")
            self.assertEqual(album.revision, 1)

    def test_save_load_and_reference_resolution_reject_reparse_final(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path = directory / "album.json"
            album = AlbumProject({}, [AlbumSide("A", 1, "side.groove.json")])
            save_album_project(album, path)

            def regular_files_are_reparse(result: os.stat_result) -> bool:
                return stat.S_ISREG(result.st_mode)

            with patch.object(
                album_module, "_is_reparse", side_effect=regular_files_are_reparse
            ):
                with self.assertRaisesRegex(ProjectValidationError, "non-reparse"):
                    load_album_project(path)
                with self.assertRaisesRegex(ProjectValidationError, "non-reparse"):
                    save_album_project(album, path, overwrite=True)
                with self.assertRaisesRegex(ProjectValidationError, "reparse point"):
                    resolve_album_reference(path, "album.json", "Reference")

    def test_legacy_requires_current_side_project_before_any_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path, original = self._copy_fixture(directory, 1)
            legacy_project = (PROJECT_FIXTURES / "schema-3.json").read_bytes()
            side_path = directory / "side.groove.json"
            side_path.write_bytes(legacy_project)
            before = {item.name: item.read_bytes() for item in directory.iterdir()}
            with self.assertRaisesRegex(
                ProjectValidationError, "project migrate PROJECT"
            ):
                migrate_album_file(path)
            after = {item.name: item.read_bytes() for item in directory.iterdir()}
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(after, before)

    def test_unknown_nested_duplicate_nonfinite_oversized_and_forward_fail_closed(self) -> None:
        legacy_v1 = decode_project_json((FIXTURES / "schema-1.json").read_bytes())
        legacy_v2 = decode_project_json((FIXTURES / "schema-2.json").read_bytes())
        mutations: tuple[
            tuple[dict[str, Any], Callable[[dict[str, Any]], None]], ...
        ] = (
            (legacy_v1, lambda data: data.__setitem__("extra", True)),
            (legacy_v1, lambda data: data["sides"][0].__setitem__("extra", True)),
            (legacy_v1, lambda data: data["artwork"].__setitem__("extra", True)),
            (legacy_v2, lambda data: data["sides"][0]["speed"].__setitem__("extra", True)),
            (legacy_v2, lambda data: data["sides"][0]["speed"]["state"].__setitem__("extra", True)),
            (legacy_v2, lambda data: data["sides"][0]["pin"].__setitem__("extra", True)),
        )
        for base, mutation in mutations:
            with self.subTest(mutation=mutation):
                payload = deepcopy(base)
                mutation(payload)
                with self.assertRaises(ProjectValidationError):
                    migrate_album_data(payload)

        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            self._write_current_side(directory)
            source = (FIXTURES / "schema-1.json").read_text(encoding="utf-8")
            cases = {
                "duplicate": source.replace(
                    '"schema": "groove-serpent.album/1",',
                    '"schema": "groove-serpent.album/1", "schema": "groove-serpent.album/1",',
                ),
                "nonfinite": source.replace("45.0", "NaN", 1),
                "overflow": source.replace("45.0", "1e400", 1),
                "forward": source.replace(
                    "groove-serpent.album/1", "groove-serpent.album/99", 1
                ),
            }
            for name, text in cases.items():
                with self.subTest(name=name):
                    path = directory / f"{name}.json"
                    path.write_text(text, encoding="utf-8")
                    before = path.read_bytes()
                    with self.assertRaises(ProjectValidationError):
                        migrate_album_file(path)
                    self.assertEqual(path.read_bytes(), before)
            oversized = directory / "oversized.json"
            oversized.write_bytes(b"{" + b" " * 512 + b"}")
            with patch.object(album_module, "MAX_ALBUM_FILE_BYTES", 256):
                with self.assertRaisesRegex(ProjectValidationError, "limit"):
                    migrate_album_file(oversized)

    def test_backup_and_receipt_collisions_are_never_overwritten(self) -> None:
        for kind in ("backup", "receipt"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as value:
                directory = Path(value)
                self._write_current_side(directory)
                path, original = self._copy_fixture(directory, 2)
                artifacts = album_migration_artifact_paths(
                    path,
                    _sha256(original),
                    "groove-serpent.album/2",
                )
                target = getattr(artifacts, kind)
                target.write_bytes(b"collision")
                before = {item.name: item.read_bytes() for item in directory.iterdir()}
                with self.assertRaises(ProjectValidationError):
                    migrate_album_file(path)
                after = {item.name: item.read_bytes() for item in directory.iterdir()}
                self.assertEqual(after, before)

    def test_candidate_backup_and_pending_hard_kill_boundaries_resume(self) -> None:
        real_write = migration_module._write_exclusive
        for stop_after in (1, 2, 3):
            with self.subTest(stop_after=stop_after), tempfile.TemporaryDirectory() as value:
                directory = Path(value)
                self._write_current_side(directory)
                path, original = self._copy_fixture(directory, 1)
                calls = 0

                def interrupted_write(target: Path, payload: bytes) -> None:
                    nonlocal calls
                    real_write(target, payload)
                    calls += 1
                    if calls == stop_after:
                        raise KeyboardInterrupt

                with patch.object(
                    migration_module, "_write_exclusive", side_effect=interrupted_write
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        migrate_album_file(path)
                self.assertEqual(path.read_bytes(), original)
                result = migrate_album_file(path)
                self.assertEqual(result.status, "recovered")
                self.assertEqual(load_album_project(path).schema, ALBUM_SCHEMA)
                self.assertEqual(
                    (directory / str(result.backup)).read_bytes(), original
                )

    def test_mismatched_partial_candidate_is_left_untouched(self) -> None:
        real_write = migration_module._write_exclusive
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            self._write_current_side(directory)
            path, original = self._copy_fixture(directory, 1)

            def stop_after_candidate(target: Path, payload: bytes) -> None:
                real_write(target, payload)
                raise KeyboardInterrupt

            with patch.object(
                migration_module, "_write_exclusive", side_effect=stop_after_candidate
            ):
                with self.assertRaises(KeyboardInterrupt):
                    migrate_album_file(path)
            artifacts = album_migration_artifact_paths(
                path, _sha256(original), "groove-serpent.album/1"
            )
            artifacts.candidate.write_bytes(b"tampered")
            with self.assertRaisesRegex(ProjectValidationError, "left untouched"):
                migrate_album_file(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(artifacts.candidate.read_bytes(), b"tampered")

    def test_replace_failure_and_post_replace_post_receipt_interruptions_recover(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            self._write_current_side(directory)
            path, original = self._copy_fixture(directory, 2)
            with patch(
                "groove_serpent.album_migration.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    migrate_album_file(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(list(directory.glob("*.tmp")))
            self.assertFalse(list(directory.glob("*.preserved")))
            self.assertEqual(migrate_album_file(path).status, "recovered")
            self.assertFalse(list(directory.glob("*.tmp")))
            self.assertFalse(list(directory.glob("*.preserved")))

        real_write = migration_module._write_exclusive
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            self._write_current_side(directory)
            path, _ = self._copy_fixture(directory, 1)

            def stop_before_receipt(target: Path, payload: bytes) -> None:
                if target.name.endswith(".receipt.json"):
                    raise KeyboardInterrupt
                real_write(target, payload)

            with patch.object(
                migration_module, "_write_exclusive", side_effect=stop_before_receipt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    migrate_album_file(path)
            self.assertEqual(load_album_project(path).schema, ALBUM_SCHEMA)
            self.assertEqual(migrate_album_file(path).status, "recovered")

        real_unlink = migration_module._unlink_verified
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            self._write_current_side(directory)
            path, _ = self._copy_fixture(directory, 2)

            def stop_after_receipt(target: Path, digest: str, maximum: int) -> None:
                if target.name.endswith(".pending.json"):
                    raise KeyboardInterrupt
                real_unlink(target, digest, maximum)

            with patch.object(
                migration_module, "_unlink_verified", side_effect=stop_after_receipt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    migrate_album_file(path)
            self.assertEqual(len(list(directory.glob("*.receipt.json"))), 1)
            self.assertEqual(migrate_album_file(path).status, "recovered")
            self.assertFalse(list(directory.glob("*.pending.json")))

    def test_album_and_side_project_toctou_conflicts_stop_before_replace(self) -> None:
        real_identities = migration_module._side_project_identities
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            self._write_current_side(directory)
            path, _ = self._copy_fixture(directory, 1)
            identity_reads = 0

            def racing_album_identity(
                album: AlbumProject, album_path: Path
            ) -> list[dict[str, object]]:
                nonlocal identity_reads
                identities = real_identities(album, album_path)
                identity_reads += 1
                if identity_reads == 2:
                    path.write_bytes(b'{"external":"album"}')
                return identities

            with patch.object(
                migration_module,
                "_side_project_identities",
                side_effect=racing_album_identity,
            ), patch(
                "groove_serpent.album_migration.os.replace"
            ) as replace_mock:
                with self.assertRaisesRegex(ProjectValidationError, "changed before"):
                    migrate_album_file(path)
                replace_mock.assert_not_called()
            self.assertEqual(path.read_bytes(), b'{"external":"album"}')

        real_identities = migration_module._side_project_identities
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            side_path = self._write_current_side(directory)
            path, original = self._copy_fixture(directory, 2)
            calls = 0

            def racing_side_identities(
                album: AlbumProject, album_path: Path
            ) -> list[dict[str, object]]:
                nonlocal calls
                calls += 1
                if calls == 2:
                    project = load_project(side_path)
                    project.metadata["external"] = "changed"
                    save_project(project, side_path)
                return real_identities(album, album_path)

            with patch.object(
                migration_module,
                "_side_project_identities",
                side_effect=racing_side_identities,
            ):
                with self.assertRaisesRegex(ProjectValidationError, "side project changed"):
                    migrate_album_file(path)
            self.assertEqual(path.read_bytes(), original)

    def test_invalid_receipt_timestamp_fails_closed(self) -> None:
        real_unlink = migration_module._unlink_verified
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            self._write_current_side(directory)
            path, _ = self._copy_fixture(directory, 1)

            def stop_after_receipt(target: Path, digest: str, maximum: int) -> None:
                if target.name.endswith(".pending.json"):
                    raise KeyboardInterrupt
                real_unlink(target, digest, maximum)

            with patch.object(
                migration_module, "_unlink_verified", side_effect=stop_after_receipt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    migrate_album_file(path)
            receipt = next(directory.glob("*.receipt.json"))
            payload = decode_project_json(receipt.read_bytes())
            payload["committed_at"] = "not-a-time"
            receipt.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ProjectValidationError, "ISO-8601"):
                migrate_album_file(path)

    def test_cli_album_migrate_json_is_portable(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            self._write_current_side(directory)
            path, _ = self._copy_fixture(directory, 1)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["album", "migrate", str(path), "--json"])
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "migrated")
        self.assertEqual(payload["album"], path.name)
        self.assertNotIn(str(directory), output.getvalue())


if __name__ == "__main__":
    unittest.main()
