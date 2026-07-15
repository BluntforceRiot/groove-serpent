from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from groove_serpent.cli import main
from groove_serpent.errors import ProjectValidationError
from groove_serpent.models import Project, SCHEMA_VERSION
from groove_serpent.project_io import decode_project_json, load_project
from groove_serpent.project_migration import (
    MIGRATION_RECEIPT_SCHEMA,
    migrate_project_data,
    migrate_project_file,
    migration_artifact_paths,
)
from groove_serpent.transaction_lock import canonical_target_path
import groove_serpent.project_migration as migration_module


FIXTURES = Path(__file__).parent / "fixtures" / "project_migrations"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class ProjectMigrationTests(unittest.TestCase):
    def _copy_fixture(self, directory: Path, schema: int) -> tuple[Path, bytes]:
        raw = (FIXTURES / f"schema-{schema}.json").read_bytes()
        path = directory / f"schema-{schema}.groove.json"
        path.write_bytes(raw)
        return path, raw

    def test_golden_v1_v2_v3_migrate_with_exact_backup_and_semantics(self) -> None:
        for schema in (1, 2, 3):
            with self.subTest(schema=schema), tempfile.TemporaryDirectory() as value:
                directory = Path(value)
                path, original = self._copy_fixture(directory, schema)
                original_data = decode_project_json(original)
                before_entries = tuple(directory.iterdir())
                with self.assertRaisesRegex(
                    ProjectValidationError, "project migrate PROJECT"
                ):
                    load_project(path)
                self.assertEqual(tuple(directory.iterdir()), before_entries)
                self.assertEqual(path.read_bytes(), original)

                result = migrate_project_file(path)
                self.assertEqual(result.status, "migrated")
                self.assertEqual(result.original_schema, schema)
                self.assertEqual(result.original_sha256, _sha256(original))
                self.assertIsNotNone(result.backup)
                self.assertIsNotNone(result.receipt)
                backup = directory / str(result.backup)
                receipt = directory / str(result.receipt)
                self.assertEqual(backup.read_bytes(), original)
                self.assertEqual(_sha256(path.read_bytes()), result.migrated_sha256)

                migrated = load_project(path)
                self.assertEqual(migrated.schema_version, SCHEMA_VERSION)
                self.assertEqual(migrated.app_version, original_data["app_version"])
                self.assertEqual(migrated.created_at, original_data["created_at"])
                self.assertEqual(migrated.updated_at, original_data["updated_at"])
                self.assertEqual(migrated.metadata, original_data["metadata"])
                self.assertEqual(migrated.analysis.music_end_seconds, 10.0)
                self.assertEqual(migrated.tracks[0].title, original_data["tracks"][0]["title"])
                self.assertEqual(migrated.source.path, original_data["source"]["path"])
                self.assertEqual(migrated.source.size_bytes, original_data["source"]["size_bytes"])
                baseline = migrated.analyzer_baseline
                self.assertIsNotNone(baseline)
                assert baseline is not None
                self.assertEqual(baseline.state_sha256, migrated.state_sha256)
                self.assertEqual(migrated.edit_history, [])
                self.assertEqual(migrated.checkpoints, [])
                self.assertEqual(migrated.revision, 1)
                if schema == 1:
                    self.assertIsNone(migrated.source.sample_count)
                    self.assertEqual(migrated.source.sha256, "")

                receipt_data = decode_project_json(receipt.read_bytes())
                self.assertEqual(receipt_data["schema"], MIGRATION_RECEIPT_SCHEMA)
                plan = receipt_data["plan"]
                self.assertEqual(plan["original_sha256"], _sha256(original))
                self.assertEqual(plan["migrated_sha256"], _sha256(path.read_bytes()))
                self.assertEqual(plan["backup"], backup.name)
                self.assertEqual(plan["receipt"], receipt.name)
                self.assertNotIn(str(directory), receipt.read_text(encoding="utf-8"))

    def test_current_project_repeat_is_a_zero_write_operation(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path, _ = self._copy_fixture(directory, 2)
            migrate_project_file(path)
            before = {
                item.name: (item.read_bytes(), item.stat().st_mtime_ns)
                for item in directory.iterdir()
            }
            result = migrate_project_file(path)
            after = {
                item.name: (item.read_bytes(), item.stat().st_mtime_ns)
                for item in directory.iterdir()
            }
        self.assertEqual(result.status, "current")
        self.assertEqual(result.original_sha256, result.migrated_sha256)
        self.assertIsNone(result.backup)
        self.assertIsNone(result.receipt)
        self.assertEqual(after, before)

    def test_valid_project_larger_than_auxiliary_limit_migrates_once(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            legacy = decode_project_json((FIXTURES / "schema-1.json").read_bytes())
            legacy["analysis"]["waveform"] = [0.0] * 240_000
            original = (json.dumps(legacy, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
            self.assertGreater(len(original), 1024 * 1024)
            path = directory / "large.groove.json"
            path.write_bytes(original)

            result = migrate_project_file(path)

            self.assertEqual(result.status, "migrated")
            self.assertEqual(result.original_sha256, _sha256(original))
            self.assertEqual((directory / str(result.backup)).read_bytes(), original)
            self.assertEqual(load_project(path).schema_version, SCHEMA_VERSION)
            self.assertFalse(
                any(
                    item.name.endswith(".candidate")
                    or item.name.endswith(".pending.json")
                    or item.name.endswith(".preserved")
                    for item in directory.iterdir()
                )
            )
            before_retry = {item.name: item.read_bytes() for item in directory.iterdir()}
            self.assertEqual(migrate_project_file(path).status, "current")
            self.assertEqual(
                {item.name: item.read_bytes() for item in directory.iterdir()},
                before_retry,
            )

    def test_long_valid_project_filename_uses_bounded_private_names(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            original = (FIXTURES / "schema-1.json").read_bytes()
            path = directory / ("a" * 205 + ".groove.json")
            path.write_bytes(original)

            result = migrate_project_file(path)

            self.assertEqual(result.status, "migrated")
            self.assertEqual(load_project(path).schema_version, SCHEMA_VERSION)
            self.assertTrue(all(len(item.name) <= 255 for item in directory.iterdir()))
            self.assertFalse(list(directory.glob("*.tmp")))
            self.assertFalse(list(directory.glob("*.preserved")))

    def test_extra_fields_are_rejected_at_root_source_track_and_current_nested_levels(self) -> None:
        legacy = decode_project_json(
            (FIXTURES / "schema-3.json").read_bytes()
        )
        mutations: tuple[Callable[[dict[str, Any]], None], ...] = (
            lambda value: value.__setitem__("surprise", True),
            lambda value: value["source"].__setitem__("surprise", True),
            lambda value: value["tracks"][0].__setitem__("surprise", True),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                payload = deepcopy(legacy)
                mutation(payload)
                with self.assertRaisesRegex(ProjectValidationError, "unexpected"):
                    migrate_project_data(payload)

        current, _ = migrate_project_data(legacy)
        current_payload = current.to_dict()
        current_payload["analysis"]["candidates"] = [
            {
                "start_seconds": 4.0,
                "end_seconds": 6.0,
                "cut_seconds": 5.0,
                "cut_sample": 5000,
                "duration_seconds": 2.0,
                "minimum_db": -60.0,
                "mean_db": -50.0,
                "contrast_db": 10.0,
                "score": 0.75,
                "selected": False,
            }
        ]
        nested_mutations = (
            lambda value: value.__setitem__("surprise", True),
            lambda value: value["source"].__setitem__("surprise", True),
            lambda value: value["tracks"][0].__setitem__("surprise", True),
            lambda value: value["settings"].pop("waveform_points"),
            lambda value: value["analysis"].pop("waveform"),
            lambda value: value["analysis"]["candidates"][0].pop("selected"),
            lambda value: value["analyzer_baseline"]["state"]["tracks"][0].pop("genre"),
        )
        for mutation in nested_mutations:
            with self.subTest(current_mutation=mutation):
                payload = deepcopy(current_payload)
                mutation(payload)
                with self.assertRaises(ProjectValidationError):
                    Project.from_dict(payload)

    def test_duplicate_nonfinite_oversized_and_forward_documents_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            fixture = (FIXTURES / "schema-1.json").read_text(encoding="utf-8")
            cases = {
                "duplicate": fixture.replace(
                    '"schema_version": 1,',
                    '"schema_version": 1, "schema_version": 1,',
                ),
                "constant": fixture.replace("10.0", "NaN", 1),
                "overflow": fixture.replace("10.0", "1e400", 1),
                "forward": fixture.replace('"schema_version": 1', '"schema_version": 99'),
            }
            for name, payload in cases.items():
                with self.subTest(name=name):
                    path = directory / f"{name}.groove.json"
                    path.write_text(payload, encoding="utf-8")
                    original = path.read_bytes()
                    with self.assertRaises(ProjectValidationError):
                        migrate_project_file(path)
                    self.assertEqual(path.read_bytes(), original)

            oversized = directory / "oversized.groove.json"
            oversized.write_bytes(b"{" + (b" " * 512) + b"}")
            with patch.object(migration_module, "MAX_PROJECT_FILE_BYTES", 256):
                with self.assertRaisesRegex(ProjectValidationError, "limit"):
                    migrate_project_file(oversized)

    def test_backup_collision_refuses_all_writes(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path, original = self._copy_fixture(directory, 1)
            artifacts = migration_artifact_paths(path, _sha256(original), 1)
            artifacts.backup.write_bytes(b"collision")
            before = {item.name: item.read_bytes() for item in directory.iterdir()}
            with self.assertRaisesRegex(ProjectValidationError, "left untouched"):
                migrate_project_file(path)
            after = {item.name: item.read_bytes() for item in directory.iterdir()}
        self.assertEqual(after, before)

    def test_receipt_collision_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path, original = self._copy_fixture(directory, 2)
            artifacts = migration_artifact_paths(path, _sha256(original), 2)
            artifacts.receipt.write_bytes(b"existing receipt")
            before = {item.name: item.read_bytes() for item in directory.iterdir()}
            with self.assertRaisesRegex(ProjectValidationError, "receipt collision"):
                migrate_project_file(path)
            after = {item.name: item.read_bytes() for item in directory.iterdir()}
        self.assertEqual(after, before)

    def test_candidate_backup_and_pending_boundaries_resume_safely(self) -> None:
        real_write = migration_module._write_exclusive
        for stop_after in (1, 2, 3):
            with self.subTest(stop_after=stop_after), tempfile.TemporaryDirectory() as value:
                directory = Path(value)
                path, original = self._copy_fixture(directory, 2)
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
                        migrate_project_file(path)
                self.assertEqual(path.read_bytes(), original)
                result = migrate_project_file(path)
                self.assertEqual(result.status, "recovered")
                self.assertEqual(load_project(path).schema_version, SCHEMA_VERSION)
                self.assertEqual(
                    (directory / str(result.backup)).read_bytes(), original
                )

    def test_mismatched_partial_artifact_is_left_for_inspection(self) -> None:
        real_write = migration_module._write_exclusive
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path, original = self._copy_fixture(directory, 1)

            def stop_after_candidate(target: Path, payload: bytes) -> None:
                real_write(target, payload)
                raise KeyboardInterrupt

            with patch.object(
                migration_module, "_write_exclusive", side_effect=stop_after_candidate
            ):
                with self.assertRaises(KeyboardInterrupt):
                    migrate_project_file(path)
            artifacts = migration_artifact_paths(path, _sha256(original), 1)
            artifacts.candidate.write_bytes(b"tampered")
            with self.assertRaisesRegex(ProjectValidationError, "left untouched"):
                migrate_project_file(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(artifacts.candidate.read_bytes(), b"tampered")

    def test_replace_failure_leaves_recoverable_pending_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path, original = self._copy_fixture(directory, 3)
            with patch(
                "groove_serpent.project_migration.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    migrate_project_file(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(list(directory.glob("*.tmp")))
            self.assertFalse(list(directory.glob("*.preserved")))
            result = migrate_project_file(path)
            self.assertEqual(result.status, "recovered")
            self.assertEqual(load_project(path).schema_version, SCHEMA_VERSION)
            self.assertFalse(list(directory.glob("*.tmp")))
            self.assertFalse(list(directory.glob("*.preserved")))

    def test_toctou_conflict_is_detected_before_replace(self) -> None:
        real_read = migration_module._read_snapshot
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path, _ = self._copy_fixture(directory, 2)
            canonical_path = canonical_target_path(path)
            pending_target_reads = 0

            def racing_read(
                target: Path, *, maximum: int | None = None
            ) -> tuple[bytes, migration_module._FileSnapshot]:
                nonlocal pending_target_reads
                pending_exists = any(directory.glob("*.pending.json"))
                if target == canonical_path and pending_exists:
                    pending_target_reads += 1
                    if pending_target_reads == 2:
                        path.write_bytes(b'{"external":"replacement"}')
                return real_read(target, maximum=maximum)

            with patch.object(
                migration_module, "_read_snapshot", side_effect=racing_read
            ), patch(
                "groove_serpent.project_migration.os.replace"
            ) as replace_mock:
                with self.assertRaisesRegex(ProjectValidationError, "changed before"):
                    migrate_project_file(path)
                replace_mock.assert_not_called()
            self.assertEqual(path.read_bytes(), b'{"external":"replacement"}')

    def test_post_replace_and_post_receipt_interruptions_recover(self) -> None:
        real_write = migration_module._write_exclusive
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path, _ = self._copy_fixture(directory, 2)

            def stop_before_receipt(target: Path, payload: bytes) -> None:
                if target.name.endswith(".receipt.json"):
                    raise KeyboardInterrupt
                real_write(target, payload)

            with patch.object(
                migration_module, "_write_exclusive", side_effect=stop_before_receipt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    migrate_project_file(path)
            self.assertEqual(load_project(path).schema_version, SCHEMA_VERSION)
            result = migrate_project_file(path)
            self.assertEqual(result.status, "recovered")

        real_unlink = migration_module._unlink_verified
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path, _ = self._copy_fixture(directory, 3)

            def stop_after_receipt(
                target: Path, expected: str, maximum: int
            ) -> None:
                if target.name.endswith(".pending.json"):
                    raise KeyboardInterrupt
                real_unlink(target, expected, maximum)

            with patch.object(
                migration_module, "_unlink_verified", side_effect=stop_after_receipt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    migrate_project_file(path)
            receipts = list(directory.glob("*.receipt.json"))
            self.assertEqual(len(receipts), 1)
            result = migrate_project_file(path)
            self.assertEqual(result.status, "recovered")
            self.assertFalse(list(directory.glob("*.pending.json")))

    def test_invalid_existing_receipt_timestamp_fails_closed(self) -> None:
        real_unlink = migration_module._unlink_verified
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path, _ = self._copy_fixture(directory, 1)

            def stop_after_receipt(
                target: Path, expected: str, maximum: int
            ) -> None:
                if target.name.endswith(".pending.json"):
                    raise KeyboardInterrupt
                real_unlink(target, expected, maximum)

            with patch.object(
                migration_module, "_unlink_verified", side_effect=stop_after_receipt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    migrate_project_file(path)
            receipt = next(directory.glob("*.receipt.json"))
            payload = decode_project_json(receipt.read_bytes())
            payload["committed_at"] = "not-a-time"
            receipt.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ProjectValidationError, "ISO-8601"):
                migrate_project_file(path)

    def test_cli_project_migrate_json_is_portable(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path, _ = self._copy_fixture(directory, 1)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["project", "migrate", str(path), "--json"])
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "migrated")
        self.assertEqual(payload["project"], path.name)
        self.assertNotIn(str(directory), output.getvalue())


if __name__ == "__main__":
    unittest.main()
