from __future__ import annotations

import subprocess
import os
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterator
from unittest.mock import patch

import groove_serpent.album as album_module
import groove_serpent.album_migration as album_migration_module
import groove_serpent.migration_commit as migration_commit_module
import groove_serpent.project_io as project_io_module
import groove_serpent.project_migration as project_migration_module
from groove_serpent.album import (
    ALBUM_SCHEMA,
    load_album_project,
    load_album_project_with_sha256,
    save_album_project,
)
from groove_serpent.album_migration import (
    AlbumMigrationResult,
    migrate_album_data,
    migrate_album_file,
)
from groove_serpent.errors import ProjectValidationError
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    SCHEMA_VERSION,
    Track,
)
from groove_serpent.project_io import (
    decode_project_json,
    load_project,
    load_project_with_sha256,
    save_project,
)
from groove_serpent.project_migration import (
    ProjectMigrationResult,
    migrate_project_data,
    migrate_project_file,
)
from groove_serpent.transaction_lock import (
    TargetWriteLease,
    canonical_target_path,
    exclusive_target_write_lease,
)


FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_FIXTURES = FIXTURES / "project_migrations"
ALBUM_FIXTURES = FIXTURES / "album_migrations"
MigrationResult = ProjectMigrationResult | AlbumMigrationResult


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


def _copy_fixture(source: Path, destination: Path) -> bytes:
    raw = source.read_bytes()
    destination.write_bytes(raw)
    return raw


def _write_current_side(directory: Path) -> Path:
    path = directory / "side.groove.json"
    save_project(_current_side_project(), path)
    return path


class MigrationWriteLeaseTests(unittest.TestCase):
    def _assert_two_migrations_serialize(
        self,
        *,
        module: ModuleType,
        migrate: Callable[[Path], MigrationResult],
        real_write: Callable[[Path, bytes], None],
        path: Path,
    ) -> None:
        first_write = threading.Event()
        release_first = threading.Event()
        second_attempting = threading.Event()
        second_acquired = threading.Event()
        results: list[MigrationResult] = []
        errors: list[BaseException] = []

        def blocked_write(target: Path, payload: bytes) -> None:
            if not first_write.is_set():
                first_write.set()
                if not release_first.wait(timeout=10.0):
                    raise AssertionError("Timed out waiting to release migration.")
            real_write(target, payload)

        @contextmanager
        def tracked_lease(target: Path) -> Iterator[TargetWriteLease]:
            is_second = threading.current_thread().name == "second-migration"
            if is_second:
                second_attempting.set()
            with exclusive_target_write_lease(target) as lease:
                if is_second:
                    second_acquired.set()
                yield lease

        def run() -> None:
            try:
                results.append(migrate(path))
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(target=run, name="first-migration")
        second = threading.Thread(target=run, name="second-migration")
        with patch.object(module, "_write_exclusive", side_effect=blocked_write), patch.object(
            module,
            "exclusive_target_write_lease",
            side_effect=tracked_lease,
        ):
            try:
                first.start()
                self.assertTrue(first_write.wait(timeout=5.0))
                second.start()
                self.assertTrue(second_attempting.wait(timeout=5.0))
                self.assertFalse(second_acquired.wait(timeout=0.1))
            finally:
                release_first.set()
                first.join(timeout=10.0)
                second.join(timeout=10.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(result.status for result in results), ["current", "migrated"])

    def test_concurrent_project_and_album_migrations_serialize(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            project_path = directory / "legacy.groove.json"
            _copy_fixture(PROJECT_FIXTURES / "schema-2.json", project_path)
            self._assert_two_migrations_serialize(
                module=project_migration_module,
                migrate=migrate_project_file,
                real_write=project_migration_module._write_exclusive,
                path=project_path,
            )
            self.assertEqual(load_project(project_path).schema_version, SCHEMA_VERSION)

        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            _write_current_side(directory)
            album_path = directory / "legacy.album.json"
            _copy_fixture(ALBUM_FIXTURES / "schema-2.json", album_path)
            self._assert_two_migrations_serialize(
                module=album_migration_module,
                migrate=migrate_album_file,
                real_write=album_migration_module._write_exclusive,
                path=album_path,
            )
            self.assertEqual(load_album_project(album_path).schema, ALBUM_SCHEMA)

    def test_project_migration_serializes_against_save_project(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path = directory / "legacy.groove.json"
            original = _copy_fixture(PROJECT_FIXTURES / "schema-2.json", path)
            replacement, _ = migrate_project_data(decode_project_json(original))
            migration_inside = threading.Event()
            release_migration = threading.Event()
            save_attempting = threading.Event()
            save_acquired = threading.Event()
            migration_results: list[ProjectMigrationResult] = []
            errors: list[BaseException] = []
            real_write = project_migration_module._write_exclusive

            def blocked_write(target: Path, payload: bytes) -> None:
                if not migration_inside.is_set():
                    migration_inside.set()
                    if not release_migration.wait(timeout=10.0):
                        raise AssertionError("Timed out waiting to release migration.")
                real_write(target, payload)

            @contextmanager
            def tracked_save_lease(target: Path) -> Iterator[TargetWriteLease]:
                save_attempting.set()
                with exclusive_target_write_lease(target) as lease:
                    save_acquired.set()
                    yield lease

            def run_migration() -> None:
                try:
                    migration_results.append(migrate_project_file(path))
                except BaseException as exc:
                    errors.append(exc)

            def run_save() -> None:
                try:
                    save_project(replacement, path)
                except BaseException as exc:
                    errors.append(exc)

            migration = threading.Thread(target=run_migration)
            save = threading.Thread(target=run_save)
            with patch.object(
                project_migration_module,
                "_write_exclusive",
                side_effect=blocked_write,
            ), patch.object(
                project_io_module,
                "exclusive_target_write_lease",
                side_effect=tracked_save_lease,
            ):
                try:
                    migration.start()
                    self.assertTrue(migration_inside.wait(timeout=5.0))
                    save.start()
                    self.assertTrue(save_attempting.wait(timeout=5.0))
                    self.assertFalse(save_acquired.wait(timeout=0.1))
                finally:
                    release_migration.set()
                    migration.join(timeout=10.0)
                    save.join(timeout=10.0)

            self.assertEqual(len(migration_results), 1)
            self.assertEqual(migration_results[0].status, "migrated")
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ProjectValidationError)
            self.assertIn("changed while waiting", str(errors[0]))
            self.assertEqual(load_project(path).schema_version, SCHEMA_VERSION)

    def test_album_migration_serializes_against_save_album_project(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            _write_current_side(directory)
            path = directory / "legacy.album.json"
            original = _copy_fixture(ALBUM_FIXTURES / "schema-2.json", path)
            replacement, _ = migrate_album_data(decode_project_json(original))
            migration_inside = threading.Event()
            release_migration = threading.Event()
            save_attempting = threading.Event()
            save_acquired = threading.Event()
            migration_results: list[AlbumMigrationResult] = []
            errors: list[BaseException] = []
            real_write = album_migration_module._write_exclusive

            def blocked_write(target: Path, payload: bytes) -> None:
                if not migration_inside.is_set():
                    migration_inside.set()
                    if not release_migration.wait(timeout=10.0):
                        raise AssertionError("Timed out waiting to release migration.")
                real_write(target, payload)

            @contextmanager
            def tracked_save_lease(target: Path) -> Iterator[TargetWriteLease]:
                save_attempting.set()
                with exclusive_target_write_lease(target) as lease:
                    save_acquired.set()
                    yield lease

            def run_migration() -> None:
                try:
                    migration_results.append(migrate_album_file(path))
                except BaseException as exc:
                    errors.append(exc)

            def run_save() -> None:
                try:
                    save_album_project(replacement, path, overwrite=True)
                except BaseException as exc:
                    errors.append(exc)

            migration = threading.Thread(target=run_migration)
            save = threading.Thread(target=run_save)
            with patch.object(
                album_migration_module,
                "_write_exclusive",
                side_effect=blocked_write,
            ), patch.object(
                album_module,
                "exclusive_target_write_lease",
                side_effect=tracked_save_lease,
            ):
                try:
                    migration.start()
                    self.assertTrue(migration_inside.wait(timeout=5.0))
                    save.start()
                    self.assertTrue(save_attempting.wait(timeout=5.0))
                    self.assertFalse(save_acquired.wait(timeout=0.1))
                finally:
                    release_migration.set()
                    migration.join(timeout=10.0)
                    save.join(timeout=10.0)

            self.assertEqual(len(migration_results), 1)
            self.assertEqual(migration_results[0].status, "migrated")
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ProjectValidationError)
            self.assertIn("changed while waiting", str(errors[0]))
            self.assertEqual(load_album_project(path).schema, ALBUM_SCHEMA)

    def test_hard_exit_releases_lease_and_pending_transactions_recover(self) -> None:
        script = (
            "import importlib, os, pathlib, sys\n"
            "module = importlib.import_module(sys.argv[1])\n"
            "real_write = module._write_exclusive\n"
            "calls = 0\n"
            "def crash_after_pending(path, payload):\n"
            "    global calls\n"
            "    real_write(path, payload)\n"
            "    calls += 1\n"
            "    if calls == 3:\n"
            "        os._exit(73)\n"
            "module._write_exclusive = crash_after_pending\n"
            "migrate = getattr(module, sys.argv[2])\n"
            "migrate(pathlib.Path(sys.argv[3]))\n"
        )
        cases: list[tuple[str, str, Callable[[Path], MigrationResult], str]] = [
            (
                "groove_serpent.project_migration",
                "migrate_project_file",
                migrate_project_file,
                "project",
            ),
            (
                "groove_serpent.album_migration",
                "migrate_album_file",
                migrate_album_file,
                "album",
            ),
        ]
        for module_name, function_name, migrate, kind in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as value:
                directory = Path(value)
                if kind == "project":
                    path = directory / "legacy.groove.json"
                    _copy_fixture(PROJECT_FIXTURES / "schema-3.json", path)
                else:
                    _write_current_side(directory)
                    path = directory / "legacy.album.json"
                    _copy_fixture(ALBUM_FIXTURES / "schema-2.json", path)
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        module_name,
                        function_name,
                        str(path),
                    ],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=20,
                )
                self.assertEqual(
                    completed.returncode,
                    73,
                    completed.stderr.decode("utf-8", errors="replace"),
                )
                self.assertEqual(migrate(path).status, "recovered")

    def test_project_migration_rejects_hardlinked_target_without_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path = directory / "legacy.groove.json"
            alias = directory / "alias.groove.json"
            _copy_fixture(PROJECT_FIXTURES / "schema-1.json", path)
            try:
                alias.hardlink_to(path)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"hardlink creation is unavailable: {exc}")
            before = {item.name: item.read_bytes() for item in directory.iterdir()}
            with self.assertRaisesRegex(ProjectValidationError, "single-link"):
                migrate_project_file(path)
            after = {item.name: item.read_bytes() for item in directory.iterdir()}
            self.assertEqual(after, before)

    def test_project_candidate_path_swap_restores_exact_original_target(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path = directory / "legacy.groove.json"
            original = _copy_fixture(PROJECT_FIXTURES / "schema-3.json", path)
            real_prepare = project_migration_module.prepare_replacement
            swapped = False

            def prepare_then_swap(target: Path, payload: bytes, **kwargs):
                nonlocal swapped
                prepared = real_prepare(target, payload, **kwargs)
                if kwargs.get("purpose") == "migration-commit" and not swapped:
                    candidate = next(directory.glob("*.candidate"))
                    os.replace(candidate, directory / "displaced-project-candidate")
                    candidate.write_bytes(b"attacker-controlled candidate bytes")
                    swapped = True
                return prepared

            with patch(
                "groove_serpent.project_migration.prepare_replacement",
                side_effect=prepare_then_swap,
            ), self.assertRaisesRegex(
                ProjectValidationError, "candidate identity changed at commit"
            ):
                migrate_project_file(path)

            self.assertTrue(swapped)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(
                next(directory.glob("*.candidate")).read_bytes(),
                b"attacker-controlled candidate bytes",
            )
            self.assertFalse(list(directory.glob("*.groove-serpent-*.tmp")))

    def test_album_candidate_path_swap_restores_exact_original_target(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            _write_current_side(directory)
            path = directory / "legacy.album.json"
            original = _copy_fixture(ALBUM_FIXTURES / "schema-2.json", path)
            real_prepare = album_migration_module.prepare_replacement
            swapped = False

            def prepare_then_swap(target: Path, payload: bytes, **kwargs):
                nonlocal swapped
                prepared = real_prepare(target, payload, **kwargs)
                if (
                    kwargs.get("purpose") == "album-migration-commit"
                    and not swapped
                ):
                    candidate = next(directory.glob("*.candidate"))
                    os.replace(candidate, directory / "displaced-album-candidate")
                    candidate.write_bytes(b"attacker-controlled album candidate")
                    swapped = True
                return prepared

            with patch(
                "groove_serpent.album_migration.prepare_replacement",
                side_effect=prepare_then_swap,
            ), self.assertRaisesRegex(
                ProjectValidationError, "candidate identity changed at commit"
            ):
                migrate_album_file(path)

            self.assertTrue(swapped)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(
                next(directory.glob("*.candidate")).read_bytes(),
                b"attacker-controlled album candidate",
            )
            self.assertFalse(list(directory.glob("*.groove-serpent-*.tmp")))

    def test_project_post_commit_identity_mismatch_restores_exact_backup(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path = directory / "legacy.groove.json"
            original = _copy_fixture(PROJECT_FIXTURES / "schema-3.json", path)
            real_prepare = project_migration_module.prepare_replacement
            swapped = False

            def prepare_then_swap_stage(target: Path, payload: bytes, **kwargs):
                nonlocal swapped
                prepared = real_prepare(target, payload, **kwargs)
                if kwargs.get("purpose") == "migration-commit" and not swapped:
                    os.replace(
                        prepared.path, directory / "displaced-bound-project-stage"
                    )
                    prepared.path.write_bytes(b"corrupt post-commit project bytes")
                    swapped = True
                return prepared

            with patch(
                "groove_serpent.project_migration.prepare_replacement",
                side_effect=prepare_then_swap_stage,
            ), self.assertRaisesRegex(
                ProjectValidationError, "descriptor-bound candidate"
            ):
                migrate_project_file(path)

            self.assertTrue(swapped)
            self.assertEqual(path.read_bytes(), original)
            self.assertNotEqual(path.read_bytes(), b"corrupt post-commit project bytes")

    def test_album_post_commit_identity_mismatch_restores_exact_backup(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            _write_current_side(directory)
            path = directory / "legacy.album.json"
            original = _copy_fixture(ALBUM_FIXTURES / "schema-2.json", path)
            real_prepare = album_migration_module.prepare_replacement
            swapped = False

            def prepare_then_swap_stage(target: Path, payload: bytes, **kwargs):
                nonlocal swapped
                prepared = real_prepare(target, payload, **kwargs)
                if (
                    kwargs.get("purpose") == "album-migration-commit"
                    and not swapped
                ):
                    os.replace(
                        prepared.path, directory / "displaced-bound-album-stage"
                    )
                    prepared.path.write_bytes(b"corrupt post-commit album bytes")
                    swapped = True
                return prepared

            with patch(
                "groove_serpent.album_migration.prepare_replacement",
                side_effect=prepare_then_swap_stage,
            ), self.assertRaisesRegex(
                ProjectValidationError, "descriptor-bound candidate"
            ):
                migrate_album_file(path)

            self.assertTrue(swapped)
            self.assertEqual(path.read_bytes(), original)
            self.assertNotEqual(path.read_bytes(), b"corrupt post-commit album bytes")

    def test_project_cleanup_quarantines_a_candidate_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path = directory / "legacy.groove.json"
            original = _copy_fixture(PROJECT_FIXTURES / "schema-3.json", path)
            victim_payload = b"irreplaceable audio capture"
            victim = directory / "irreplaceable.flac"
            victim.write_bytes(victim_payload)
            displaced = directory / "displaced-verified-candidate"
            real_quarantine = migration_commit_module.quarantine_path_no_replace
            swapped = False

            def quarantine_after_swap(target: Path, *, purpose: str) -> Path:
                nonlocal swapped
                if target.name.endswith(".candidate") and not swapped:
                    os.replace(target, displaced)
                    os.replace(victim, target)
                    swapped = True
                return real_quarantine(target, purpose=purpose)

            with patch(
                "groove_serpent.migration_commit.quarantine_path_no_replace",
                side_effect=quarantine_after_swap,
            ), self.assertRaisesRegex(
                ProjectValidationError, "candidate identity changed at commit"
            ):
                migrate_project_file(path)

            self.assertTrue(swapped)
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(displaced.exists())
            preserved = [
                item
                for item in directory.rglob("*")
                if item.is_file() and item.read_bytes() == victim_payload
            ]
            self.assertEqual(len(preserved), 1)

    def test_album_cleanup_quarantines_a_candidate_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            _write_current_side(directory)
            path = directory / "legacy.album.json"
            original = _copy_fixture(ALBUM_FIXTURES / "schema-2.json", path)
            victim_payload = b"irreplaceable album capture"
            victim = directory / "irreplaceable.flac"
            victim.write_bytes(victim_payload)
            displaced = directory / "displaced-verified-album-candidate"
            real_quarantine = migration_commit_module.quarantine_path_no_replace
            swapped = False

            def quarantine_after_swap(target: Path, *, purpose: str) -> Path:
                nonlocal swapped
                if target.name.endswith(".candidate") and not swapped:
                    os.replace(target, displaced)
                    os.replace(victim, target)
                    swapped = True
                return real_quarantine(target, purpose=purpose)

            with patch(
                "groove_serpent.migration_commit.quarantine_path_no_replace",
                side_effect=quarantine_after_swap,
            ), self.assertRaisesRegex(
                ProjectValidationError, "candidate identity changed at commit"
            ):
                migrate_album_file(path)

            self.assertTrue(swapped)
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(displaced.exists())
            preserved = [
                item
                for item in directory.rglob("*")
                if item.is_file() and item.read_bytes() == victim_payload
            ]
            self.assertEqual(len(preserved), 1)

    def test_prepared_stage_discard_preserves_a_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            target = directory / "project.groove.json"
            target.write_bytes(b"original")
            prepared = migration_commit_module.prepare_replacement(
                target,
                b"candidate",
                maximum=1024,
                purpose="discard-race-test",
            )
            victim_payload = b"must not be deleted"
            victim = directory / "victim.flac"
            victim.write_bytes(victim_payload)
            displaced = directory / "displaced-prepared-stage"
            real_quarantine = migration_commit_module.quarantine_path_no_replace
            swapped = False

            def quarantine_after_swap(path: Path, *, purpose: str) -> Path:
                nonlocal swapped
                if path == prepared.path and not swapped:
                    os.replace(path, displaced)
                    os.replace(victim, path)
                    swapped = True
                return real_quarantine(path, purpose=purpose)

            with patch(
                "groove_serpent.migration_commit.quarantine_path_no_replace",
                side_effect=quarantine_after_swap,
            ):
                prepared.discard()

            self.assertTrue(swapped)
            self.assertTrue(prepared.handle.closed)
            self.assertEqual(displaced.read_bytes(), b"candidate")
            preserved = [
                item
                for item in directory.rglob("*")
                if item.is_file() and item.read_bytes() == victim_payload
            ]
            self.assertEqual(len(preserved), 1)

    def test_prepared_stage_discard_removes_the_exact_owned_stage(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            target = directory / "project.groove.json"
            prepared = migration_commit_module.prepare_replacement(
                target,
                b"candidate",
                maximum=1024,
                purpose="discard-owned-stage-test",
            )
            stage = prepared.path

            prepared.discard()

            self.assertTrue(prepared.handle.closed)
            self.assertFalse(os.path.lexists(stage))
            self.assertEqual(tuple(directory.iterdir()), ())

    def test_prepared_stage_discard_reports_a_blocked_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            target = directory / "project.groove.json"
            prepared = migration_commit_module.prepare_replacement(
                target,
                b"candidate",
                maximum=1024,
                purpose="discard-blocked-stage-test",
            )
            stage = prepared.path

            with patch(
                "groove_serpent.migration_commit.quarantine_path_no_replace",
                side_effect=PermissionError("sharing violation"),
            ):
                retained = prepared.discard()

            self.assertEqual(retained, stage)
            self.assertTrue(prepared.handle.closed)
            self.assertEqual(stage.read_bytes(), b"candidate")

    def test_prepare_open_failure_reports_its_retained_stage(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            target = directory / "project.groove.json"
            with patch(
                "groove_serpent.migration_commit._open_bound_read",
                side_effect=PermissionError("scanner blocked open"),
            ), self.assertRaises(PermissionError) as raised:
                migration_commit_module.prepare_replacement(
                    target,
                    b"candidate",
                    maximum=1024,
                    purpose="open-failure-stage-test",
                )

            stages = list(directory.glob(".groove-serpent-stage-*.tmp"))
            self.assertEqual(len(stages), 1)
            self.assertEqual(stages[0].read_bytes(), b"candidate")
            notes = getattr(raised.exception, "__notes__", [])
            self.assertTrue(any(stages[0].name in note for note in notes), notes)

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_prepared_stage_discard_closes_on_a_real_windows_reader(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            target = directory / "project.groove.json"
            prepared = migration_commit_module.prepare_replacement(
                target,
                b"candidate",
                maximum=1024,
                purpose="discard-reader-stage-test",
            )
            stage = prepared.path

            with stage.open("rb"):
                retained = prepared.discard()

            self.assertEqual(retained, stage)
            self.assertTrue(prepared.handle.closed)
            self.assertEqual(stage.read_bytes(), b"candidate")

    def test_project_rollback_preserves_an_unowned_target_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path = directory / "legacy.groove.json"
            original = _copy_fixture(PROJECT_FIXTURES / "schema-3.json", path)
            victim_payload = b"irreplaceable rollback conflict"
            victim = directory / "irreplaceable.flac"
            victim.write_bytes(victim_payload)
            held_migrated = directory / "held-migrated-project"
            real_replace = project_migration_module._replace_sibling
            swapped = False

            def replace_then_swap_target(source: Path, destination: Path) -> None:
                nonlocal swapped
                real_replace(source, destination)
                if Path(destination).name == path.name and not swapped:
                    os.replace(destination, held_migrated)
                    os.replace(victim, destination)
                    swapped = True

            with patch(
                "groove_serpent.project_migration._replace_sibling",
                side_effect=replace_then_swap_target,
            ), self.assertRaisesRegex(
                ProjectValidationError, "descriptor-bound candidate"
            ):
                migrate_project_file(path)

            self.assertTrue(swapped)
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(held_migrated.exists())
            preserved = [
                item
                for item in directory.rglob("*")
                if item.is_file() and item.read_bytes() == victim_payload
            ]
            self.assertEqual(len(preserved), 1)

    def test_post_replace_failure_fences_save_until_project_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            path = directory / "legacy.groove.json"
            _copy_fixture(PROJECT_FIXTURES / "schema-3.json", path)
            canonical = canonical_target_path(path)
            real_replace = os.replace
            replaced = False

            def replace_then_fail(
                source: Path | str, destination: Path | str, **kwargs
            ) -> None:
                nonlocal replaced
                real_replace(source, destination, **kwargs)
                if Path(destination).name == canonical.name and not replaced:
                    replaced = True
                    raise RuntimeError("fault after project replacement")

            with patch(
                "groove_serpent.project_migration.os.replace",
                side_effect=replace_then_fail,
            ):
                with self.assertRaisesRegex(RuntimeError, "after project replacement"):
                    migrate_project_file(path)

            self.assertTrue(replaced)
            self.assertEqual(len(list(directory.glob("*.pending.json"))), 1)
            stale, stale_sha256 = load_project_with_sha256(path)
            stale.metadata["artist"] = "Blocked while pending"
            before = path.read_bytes()
            with self.assertRaisesRegex(
                ProjectValidationError, "incomplete project migration is pending"
            ):
                save_project(
                    stale,
                    path,
                    expected_existing_sha256=stale_sha256,
                )
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(migrate_project_file(path).status, "recovered")
            self.assertFalse(list(directory.glob("*.pending.json")))

            current, current_sha256 = load_project_with_sha256(path)
            current.metadata["artist"] = "Allowed after recovery"
            save_project(
                current,
                path,
                expected_existing_sha256=current_sha256,
            )
            self.assertEqual(load_project(path).revision, 2)

    def test_post_replace_failure_fences_save_until_album_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            directory = Path(value)
            _write_current_side(directory)
            path = directory / "legacy.album.json"
            _copy_fixture(ALBUM_FIXTURES / "schema-2.json", path)
            canonical = canonical_target_path(path)
            real_replace = os.replace
            replaced = False

            def replace_then_fail(
                source: Path | str, destination: Path | str, **kwargs
            ) -> None:
                nonlocal replaced
                real_replace(source, destination, **kwargs)
                if Path(destination).name == canonical.name and not replaced:
                    replaced = True
                    raise RuntimeError("fault after album replacement")

            with patch(
                "groove_serpent.album_migration.os.replace",
                side_effect=replace_then_fail,
            ):
                with self.assertRaisesRegex(RuntimeError, "after album replacement"):
                    migrate_album_file(path)

            self.assertTrue(replaced)
            self.assertEqual(len(list(directory.glob("*.pending.json"))), 1)
            stale, stale_sha256 = load_album_project_with_sha256(path)
            stale.metadata["title"] = "Blocked while pending"
            before = path.read_bytes()
            with self.assertRaisesRegex(
                ProjectValidationError, "incomplete album migration is pending"
            ):
                save_album_project(
                    stale,
                    path,
                    overwrite=True,
                    expected_existing_sha256=stale_sha256,
                )
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(migrate_album_file(path).status, "recovered")
            self.assertFalse(list(directory.glob("*.pending.json")))

            current, current_sha256 = load_album_project_with_sha256(path)
            current.metadata["title"] = "Allowed after recovery"
            save_album_project(
                current,
                path,
                overwrite=True,
                expected_existing_sha256=current_sha256,
            )
            self.assertEqual(load_album_project(path).revision, 2)


if __name__ == "__main__":
    unittest.main()
