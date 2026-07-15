from __future__ import annotations

import errno
import os
import importlib
import subprocess
import sys
import tempfile
import threading
import unittest
from hashlib import sha256
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

import groove_serpent.project_io as project_io_module
import groove_serpent.album as album_module
import groove_serpent.transaction_lock as transaction_lock_module
from groove_serpent.album import (
    MAX_ALBUM_REVISION,
    AlbumProject,
    AlbumSide,
    load_album_project,
    save_album_project,
)
from groove_serpent.errors import ProjectValidationError
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    MAX_PROJECT_REVISION,
    Project,
    Track,
)
from groove_serpent.project_io import load_project, save_project
from groove_serpent.transaction_lock import (
    TargetWriteLease,
    canonical_target_path,
    exclusive_target_write_lease,
    target_lock_path,
)


def _project() -> Project:
    return Project(
        source=AudioSource(
            path="source.flac",
            filename="source.flac",
            size_bytes=100,
            modified_ns=1,
            duration_seconds=1.0,
            sample_rate=1_000,
            channels=2,
            codec_name="flac",
            bits_per_raw_sample=24,
            sample_format="s32",
            sample_count=1_000,
            sha256="1" * 64,
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
        tracks=[
            Track(
                number=1,
                title="Track",
                start_sample=0,
                end_sample=1_000,
                start_seconds=0.0,
                end_seconds=1.0,
            )
        ],
    )


def _album(title: str = "Album") -> AlbumProject:
    return AlbumProject(
        metadata={"album": title},
        sides=[AlbumSide(label="A", order=1, project="side.groove.json")],
    )


def _windows_short_path(path: Path) -> Path:
    ctypes: Any = importlib.import_module("ctypes")
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    get_short_path_name: Any = kernel32.GetShortPathNameW
    required = int(get_short_path_name(os.fspath(path), None, 0))
    if required <= 0:
        raise OSError("GetShortPathNameW is unavailable for this test path.")
    buffer: Any = ctypes.create_unicode_buffer(required + 1)
    written = int(get_short_path_name(os.fspath(path), buffer, len(buffer)))
    if written <= 0 or written >= len(buffer):
        raise OSError("GetShortPathNameW could not return a bounded path.")
    return Path(str(buffer.value))


class TransactionLockTests(unittest.TestCase):
    def test_existing_lock_still_requires_atomic_no_replace_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            target = Path(directory_value) / "project.groove.json"
            with exclusive_target_write_lease(target):
                pass
            lock = target_lock_path(target)
            before = lock.read_bytes()
            with mock.patch.object(
                transaction_lock_module,
                "probe_atomic_no_replace",
                side_effect=OSError(errno.ENOTSUP, "unsupported filesystem"),
            ):
                with self.assertRaisesRegex(
                    ProjectValidationError, "Mixed Windows/WSL access is unsupported"
                ):
                    with exclusive_target_write_lease(target):
                        pass
            self.assertEqual(lock.read_bytes(), before)

    def test_windows_existing_final_short_and_long_names_share_one_lock(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows 8.3 aliases are Windows-specific.")
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "Groove Serpent Alias Test Directory"
            root.mkdir()
            target = root / "A Very Long Project Filename.groove.json"
            save_project(_project(), target)
            try:
                short_target = _windows_short_path(target)
            except OSError as exc:
                self.skipTest(str(exc))
            if "~" not in short_target.name or short_target == target:
                self.skipTest("This volume did not create a final-component 8.3 alias.")
            self.assertTrue(short_target.samefile(target))
            self.assertEqual(
                canonical_target_path(short_target),
                canonical_target_path(target),
            )
            self.assertEqual(target_lock_path(short_target), target_lock_path(target))

    def test_lock_name_is_bounded_deterministic_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            target = root / ("x" * 220 + ".groove.json")
            first = target_lock_path(target)
            second = target_lock_path(target)
            self.assertEqual(first, second)
            self.assertEqual(first.parent, root.resolve(strict=True))
            self.assertLessEqual(len(first.name), 255)
            self.assertTrue(first.name.startswith(".groove-serpent-write-"))

    def test_portable_equivalent_targets_share_one_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            composed = root / "caf\N{LATIN SMALL LETTER E WITH ACUTE}.groove-album.json"
            decomposed = root / "cafe\N{COMBINING ACUTE ACCENT}.groove-album.json"
            self.assertNotEqual(composed.name, decomposed.name)
            self.assertEqual(target_lock_path(composed), target_lock_path(decomposed))

    def test_second_thread_times_out_while_first_holds_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            target = Path(directory_value) / "album.groove.json"
            errors: list[BaseException] = []

            def contend() -> None:
                try:
                    with exclusive_target_write_lease(
                        target, timeout_seconds=0.05
                    ):
                        pass
                except BaseException as exc:
                    errors.append(exc)

            with exclusive_target_write_lease(target):
                thread = threading.Thread(target=contend)
                thread.start()
                thread.join(timeout=2.0)
                self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ProjectValidationError)
            self.assertIn("Another Groove Serpent process", str(errors[0]))

    def test_operating_system_releases_lease_after_hard_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            target = root / "project.groove.json"
            ready = root / "ready"
            script = (
                "import os, pathlib, sys\n"
                "from groove_serpent.transaction_lock import "
                "exclusive_target_write_lease\n"
                "target=pathlib.Path(sys.argv[1])\n"
                "ready=pathlib.Path(sys.argv[2])\n"
                "with exclusive_target_write_lease(target):\n"
                "    ready.write_text('held', encoding='utf-8')\n"
                "    os._exit(91)\n"
            )
            completed = subprocess.run(
                [sys.executable, "-c", script, str(target), str(ready)],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
            )
            self.assertEqual(
                completed.returncode,
                91,
                completed.stderr.decode("utf-8", errors="replace"),
            )
            self.assertEqual(ready.read_text(encoding="utf-8"), "held")
            with exclusive_target_write_lease(target, timeout_seconds=1.0):
                pass

    def test_project_save_rejects_stale_revision_after_first_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            path = Path(directory_value) / "project.groove.json"
            initial = _project()
            save_project(initial, path)
            first = load_project(path)
            stale = load_project(path)
            first.metadata["artist"] = "First writer"
            save_project(first, path)
            stale.metadata["artist"] = "Stale writer"
            with self.assertRaisesRegex(
                ProjectValidationError, "revision changed"
            ):
                save_project(stale, path)
            self.assertEqual(load_project(path).metadata["artist"], "First writer")

    def test_two_absent_path_writers_cannot_both_create(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            path = Path(directory_value) / "project.groove.json"
            barrier = threading.Barrier(2)
            real_lease = exclusive_target_write_lease

            @contextmanager
            def synchronized_lease(target: Path) -> Iterator[TargetWriteLease]:
                barrier.wait(timeout=5.0)
                with real_lease(target) as lease:
                    yield lease

            outcomes: list[tuple[str, str]] = []

            def save_named(name: str) -> None:
                project = _project()
                project.metadata["artist"] = name
                try:
                    save_project(project, path)
                except ProjectValidationError as exc:
                    outcomes.append(("rejected", str(exc)))
                else:
                    outcomes.append(("saved", name))

            with mock.patch.object(
                project_io_module,
                "exclusive_target_write_lease",
                side_effect=synchronized_lease,
            ):
                threads = [
                    threading.Thread(target=save_named, args=(name,))
                    for name in ("First", "Second")
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10.0)
                    self.assertFalse(thread.is_alive())

            self.assertEqual(sum(item[0] == "saved" for item in outcomes), 1)
            self.assertEqual(sum(item[0] == "rejected" for item in outcomes), 1)
            rejection = next(item[1] for item in outcomes if item[0] == "rejected")
            self.assertIn("existence changed", rejection)
            self.assertEqual(load_project(path).revision, 1)

    def test_portable_equivalent_project_creators_cannot_both_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            composed = root / "caf\N{LATIN SMALL LETTER E WITH ACUTE}.groove.json"
            decomposed = root / "cafe\N{COMBINING ACUTE ACCENT}.groove.json"
            try:
                composed.write_text("probe", encoding="utf-8")
                decomposed.write_text("probe", encoding="utf-8")
                if composed.is_file() and decomposed.is_file() and not composed.samefile(
                    decomposed
                ):
                    self.skipTest(
                        "Filesystem allows both portable-equivalent names "
                        "as distinct final files."
                    )
            finally:
                for candidate in (composed, decomposed):
                    try:
                        candidate.unlink()
                    except FileNotFoundError:
                        pass
            barrier = threading.Barrier(2)
            real_lease = exclusive_target_write_lease

            @contextmanager
            def synchronized_lease(target: Path) -> Iterator[TargetWriteLease]:
                barrier.wait(timeout=5.0)
                with real_lease(target) as lease:
                    yield lease

            outcomes: list[str] = []

            def create(path: Path, artist: str) -> None:
                project = _project()
                project.metadata["artist"] = artist
                try:
                    save_project(
                        project,
                        path,
                        expected_existing_sha256=None,
                    )
                except ProjectValidationError:
                    outcomes.append("rejected")
                else:
                    outcomes.append("saved")

            with mock.patch.object(
                project_io_module,
                "exclusive_target_write_lease",
                side_effect=synchronized_lease,
            ):
                threads = [
                    threading.Thread(target=create, args=(composed, "Composed")),
                    threading.Thread(target=create, args=(decomposed, "Decomposed")),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10.0)
                    self.assertFalse(thread.is_alive())

            self.assertEqual(outcomes.count("saved"), 1)
            self.assertEqual(outcomes.count("rejected"), 1)
            self.assertEqual(int(composed.exists()) + int(decomposed.exists()), 1)

    def test_reserved_lock_link_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            target = root / "project.groove.json"
            lock = target_lock_path(target)
            owner = root / "owner.txt"
            owner.write_text("owner", encoding="utf-8")
            try:
                lock.symlink_to(owner)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            with self.assertRaisesRegex(
                ProjectValidationError,
                "regular, non-reparse",
            ):
                with exclusive_target_write_lease(target):
                    pass
            self.assertEqual(owner.read_text(encoding="utf-8"), "owner")

    def test_held_lease_rejects_lock_magic_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            target = Path(directory_value) / "project.groove.json"
            with self.assertRaisesRegex(ProjectValidationError, "unexpected contents"):
                with exclusive_target_write_lease(target) as lease:
                    lease.assert_current()
                    os.lseek(lease.descriptor, 0, os.SEEK_SET)
                    os.write(lease.descriptor, b"X")

    def test_explicit_project_expectation_rejects_stale_caller_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            path = Path(directory_value) / "project.groove.json"
            first = _project()
            save_project(first, path, expected_existing_sha256=None)
            stale_digest = "0" * 64
            replacement = load_project(path)
            replacement.metadata["artist"] = "Replacement"
            with self.assertRaisesRegex(
                ProjectValidationError, "changed after the caller loaded"
            ):
                save_project(
                    replacement,
                    path,
                    expected_existing_sha256=stale_digest,
                )
            current_digest = sha256(path.read_bytes()).hexdigest()
            save_project(
                replacement,
                path,
                expected_existing_sha256=current_digest,
            )
            self.assertEqual(load_project(path).metadata["artist"], "Replacement")

    def test_project_and_album_hardlink_targets_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            project_path = root / "project.groove.json"
            project_alias = root / "project-alias.groove.json"
            album_path = root / "album.groove-album.json"
            album_alias = root / "album-alias.groove-album.json"
            save_project(_project(), project_path)
            save_album_project(_album(), album_path)
            try:
                project_alias.hardlink_to(project_path)
                album_alias.hardlink_to(album_path)
            except OSError as exc:
                self.skipTest(f"hardlink creation is unavailable: {exc}")
            with self.assertRaisesRegex(ProjectValidationError, "single-link"):
                load_project(project_path)
            with self.assertRaisesRegex(ProjectValidationError, "single-link"):
                load_album_project(album_path)

    def test_revision_exhaustion_preserves_project_and_album_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            project_path = root / "project.groove.json"
            project = _project()
            project.revision = MAX_PROJECT_REVISION
            save_project(project, project_path)
            project_bytes = project_path.read_bytes()
            with self.assertRaisesRegex(ProjectValidationError, "exhausted"):
                save_project(project, project_path)
            self.assertEqual(project_path.read_bytes(), project_bytes)
            self.assertEqual(project.revision, MAX_PROJECT_REVISION)

            album_path = root / "album.groove-album.json"
            album = _album()
            album.revision = MAX_ALBUM_REVISION
            save_album_project(album, album_path)
            album_bytes = album_path.read_bytes()
            with self.assertRaisesRegex(ProjectValidationError, "exhausted"):
                save_album_project(album, album_path, overwrite=True)
            self.assertEqual(album_path.read_bytes(), album_bytes)
            self.assertEqual(album.revision, MAX_ALBUM_REVISION)

    def test_portable_equivalent_album_creators_cannot_both_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            composed = root / "caf\N{LATIN SMALL LETTER E WITH ACUTE}.groove-album.json"
            decomposed = root / "cafe\N{COMBINING ACUTE ACCENT}.groove-album.json"
            try:
                composed.write_text("probe", encoding="utf-8")
                decomposed.write_text("probe", encoding="utf-8")
                if composed.is_file() and decomposed.is_file() and not composed.samefile(
                    decomposed
                ):
                    self.skipTest(
                        "Filesystem allows both portable-equivalent names "
                        "as distinct final files."
                    )
            finally:
                for candidate in (composed, decomposed):
                    try:
                        candidate.unlink()
                    except FileNotFoundError:
                        pass
            barrier = threading.Barrier(2)
            real_lease = exclusive_target_write_lease

            @contextmanager
            def synchronized_lease(target: Path) -> Iterator[TargetWriteLease]:
                barrier.wait(timeout=5.0)
                with real_lease(target) as lease:
                    yield lease

            outcomes: list[str] = []

            def create(path: Path, title: str) -> None:
                try:
                    save_album_project(
                        _album(title),
                        path,
                        expected_existing_sha256=None,
                    )
                except ProjectValidationError:
                    outcomes.append("rejected")
                else:
                    outcomes.append("saved")

            with mock.patch.object(
                album_module,
                "exclusive_target_write_lease",
                side_effect=synchronized_lease,
            ):
                threads = [
                    threading.Thread(target=create, args=(composed, "Composed")),
                    threading.Thread(target=create, args=(decomposed, "Decomposed")),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10.0)
                    self.assertFalse(thread.is_alive())

            self.assertEqual(outcomes.count("saved"), 1)
            self.assertEqual(outcomes.count("rejected"), 1)
            self.assertEqual(int(composed.exists()) + int(decomposed.exists()), 1)


if __name__ == "__main__":
    unittest.main()
