from __future__ import annotations

import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from groove_serpent import atomic_create
from groove_serpent.atomic_create import probe_atomic_no_replace, rename_no_replace


class AtomicCreateTests(unittest.TestCase):
    def test_probe_exercises_nearest_existing_parent_without_residue(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            before = tuple(root.iterdir())

            exercised = probe_atomic_no_replace(root / "future" / "nested")

            self.assertEqual(exercised, root)
            self.assertEqual(tuple(root.iterdir()), before)

    def test_probe_never_deletes_a_racing_destination_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            destinations: list[Path] = []
            winner = b"independent destination owner"

            def lose_race(source: Path, destination: Path) -> None:
                destination.write_bytes(winner)
                destinations.append(destination)
                raise FileExistsError(errno.EEXIST, "racing destination")

            with mock.patch(
                "groove_serpent.atomic_create.rename_no_replace",
                side_effect=lose_race,
            ):
                with self.assertRaises(FileExistsError):
                    probe_atomic_no_replace(root)

            self.assertEqual(len(destinations), 1)
            self.assertEqual(destinations[0].read_bytes(), winner)
            self.assertEqual(tuple(root.iterdir()), (destinations[0],))

    def test_probe_leaves_a_substituted_source_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            sources: list[Path] = []
            replacement = b"substituted source owned by another writer"

            def substitute_source(source: Path, destination: Path) -> None:
                del destination
                source.unlink()
                source.write_bytes(replacement)
                sources.append(source)
                raise OSError(errno.ENOTSUP, "simulated unsupported rename")

            with mock.patch(
                "groove_serpent.atomic_create.rename_no_replace",
                side_effect=substitute_source,
            ):
                with self.assertRaises(OSError) as raised:
                    probe_atomic_no_replace(root)

            self.assertTrue(
                any(
                    "cleanup lost ownership" in note
                    for note in getattr(raised.exception, "__notes__", ())
                )
            )
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].read_bytes(), replacement)
            self.assertEqual(tuple(root.iterdir()), (sources[0],))

    def test_probe_leaves_a_substituted_published_path_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            destinations: list[Path] = []
            replacement = b"substituted published path owned by another writer"
            real_rename = rename_no_replace

            def substitute_destination(source: Path, destination: Path) -> None:
                real_rename(source, destination)
                destination.unlink()
                destination.write_bytes(replacement)
                destinations.append(destination)

            with mock.patch(
                "groove_serpent.atomic_create.rename_no_replace",
                side_effect=substitute_destination,
            ):
                with self.assertRaisesRegex(OSError, "preserve one exact file"):
                    probe_atomic_no_replace(root)

            self.assertEqual(len(destinations), 1)
            self.assertEqual(destinations[0].read_bytes(), replacement)
            self.assertEqual(tuple(root.iterdir()), (destinations[0],))

    def test_probe_refreshes_cleanup_identity_when_fsync_is_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)

            with mock.patch(
                "groove_serpent.atomic_create.os.fsync",
                side_effect=KeyboardInterrupt("simulated fsync interruption"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    probe_atomic_no_replace(root)

            self.assertEqual(tuple(root.iterdir()), ())

    def test_probe_refreshes_partial_payload_when_write_is_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            real_fdopen = atomic_create.os.fdopen

            class PartialWriteHandle:
                def __init__(self, handle: object) -> None:
                    self.handle = handle

                def __enter__(self) -> PartialWriteHandle:
                    return self

                def __exit__(self, *args: object) -> object:
                    return self.handle.__exit__(*args)  # type: ignore[attr-defined]

                def write(self, payload: bytes) -> None:
                    self.handle.write(payload[:11])  # type: ignore[attr-defined]
                    self.handle.flush()  # type: ignore[attr-defined]
                    raise KeyboardInterrupt("simulated partial write")

                def flush(self) -> None:
                    self.handle.flush()  # type: ignore[attr-defined]

                def fileno(self) -> int:
                    return int(self.handle.fileno())  # type: ignore[attr-defined]

            def partial_fdopen(*args: object, **kwargs: object) -> PartialWriteHandle:
                return PartialWriteHandle(real_fdopen(*args, **kwargs))

            with mock.patch.object(
                atomic_create.os,
                "fdopen",
                side_effect=partial_fdopen,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    probe_atomic_no_replace(root)

            self.assertEqual(tuple(root.iterdir()), ())

    def test_probe_refreshes_payload_after_flush_is_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            real_fdopen = atomic_create.os.fdopen

            class InterruptedFlushHandle:
                def __init__(self, handle: object) -> None:
                    self.handle = handle

                def __enter__(self) -> InterruptedFlushHandle:
                    return self

                def __exit__(self, *args: object) -> object:
                    return self.handle.__exit__(*args)  # type: ignore[attr-defined]

                def write(self, payload: bytes) -> object:
                    return self.handle.write(payload)  # type: ignore[attr-defined]

                def flush(self) -> None:
                    raise KeyboardInterrupt("simulated flush interruption")

                def fileno(self) -> int:
                    return int(self.handle.fileno())  # type: ignore[attr-defined]

            def interrupted_fdopen(
                *args: object,
                **kwargs: object,
            ) -> InterruptedFlushHandle:
                return InterruptedFlushHandle(real_fdopen(*args, **kwargs))

            with mock.patch.object(
                atomic_create.os,
                "fdopen",
                side_effect=interrupted_fdopen,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    probe_atomic_no_replace(root)

            self.assertEqual(tuple(root.iterdir()), ())

    def test_probe_cleans_destination_when_rename_publishes_then_interrupts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            real_rename = rename_no_replace

            def publish_then_interrupt(source: Path, destination: Path) -> None:
                real_rename(source, destination)
                raise KeyboardInterrupt("simulated post-rename interruption")

            with mock.patch(
                "groove_serpent.atomic_create.rename_no_replace",
                side_effect=publish_then_interrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    probe_atomic_no_replace(root)

            self.assertEqual(tuple(root.iterdir()), ())

    def test_probe_raises_when_cleanup_fails_without_an_active_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)

            with mock.patch.object(
                atomic_create,
                "_remove_owned_probe_path",
                return_value=False,
            ):
                with self.assertRaisesRegex(OSError, "cleanup lost ownership"):
                    probe_atomic_no_replace(root)

    @unittest.skipIf(os.name == "nt", "POSIX unsupported-rename fallback")
    def test_probe_cleans_owned_source_when_cleanup_rename_is_unsupported(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            unsupported = OSError(
                errno.ENOTSUP,
                "simulated filesystem without no-replace rename",
            )

            with mock.patch(
                "groove_serpent.atomic_create.rename_no_replace",
                side_effect=unsupported,
            ), mock.patch.object(
                atomic_create,
                "_rename_no_replace_for_cleanup",
                side_effect=unsupported,
            ):
                with self.assertRaises(OSError) as raised:
                    probe_atomic_no_replace(root)

            self.assertEqual(raised.exception.errno, errno.ENOTSUP)
            self.assertEqual(tuple(root.iterdir()), ())

    @unittest.skipIf(os.name == "nt", "POSIX unsupported-rename fallback")
    def test_unsupported_cleanup_fallback_preserves_substituted_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            replacement = b"independent source owner"
            sources: list[Path] = []

            def substitute_source(source: Path, destination: Path) -> None:
                del destination
                source.unlink()
                source.write_bytes(replacement)
                sources.append(source)
                raise OSError(errno.ENOTSUP, "simulated unsupported rename")

            with mock.patch(
                "groove_serpent.atomic_create.rename_no_replace",
                side_effect=substitute_source,
            ), mock.patch.object(
                atomic_create,
                "_rename_no_replace_for_cleanup",
                side_effect=OSError(errno.ENOTSUP, "unsupported cleanup rename"),
            ):
                with self.assertRaises(OSError) as raised:
                    probe_atomic_no_replace(root)

            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].read_bytes(), replacement)
            self.assertTrue(
                any(
                    "cleanup lost ownership" in note
                    for note in getattr(raised.exception, "__notes__", ())
                )
            )
            self.assertEqual(tuple(root.iterdir()), (sources[0],))

    @unittest.skipUnless(
        sys.platform.startswith("linux") and Path("/mnt/n").is_dir(),
        "real WSL /mnt/n mount is unavailable",
    )
    def test_real_wsl_mnt_n_probe_never_leaves_residue(self) -> None:
        try:
            temporary = tempfile.TemporaryDirectory(dir="/mnt/n")
        except OSError as exc:
            self.skipTest(f"cannot create a real /mnt/n probe directory: {exc}")
        with temporary as directory_value:
            root = Path(directory_value)
            before = tuple(root.iterdir())

            try:
                probe_atomic_no_replace(root)
            except OSError as exc:
                self.assertIn(exc.errno, atomic_create._UNSUPPORTED_NO_REPLACE_ERRNOS)

            self.assertEqual(tuple(root.iterdir()), before)

    @unittest.skipIf(os.name == "nt", "POSIX quarantine regression")
    def test_posix_cleanup_quarantine_preserves_a_swapped_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            displaced = root / "displaced-owned-probe"
            replacement = b"independent cleanup winner"
            swapped_path: Path | None = None
            real_cleanup_rename = atomic_create._rename_no_replace_for_cleanup
            swapped = False

            def swap_before_quarantine(source: Path, destination: Path) -> None:
                nonlocal swapped, swapped_path
                if not swapped and source.name.endswith(".published"):
                    swapped = True
                    swapped_path = source
                    os.rename(source, displaced)
                    source.write_bytes(replacement)
                real_cleanup_rename(source, destination)

            with mock.patch.object(
                atomic_create,
                "_rename_no_replace_for_cleanup",
                side_effect=swap_before_quarantine,
            ), self.assertRaisesRegex(OSError, "cleanup lost ownership"):
                probe_atomic_no_replace(root)

            self.assertIsNotNone(swapped_path)
            assert swapped_path is not None
            self.assertEqual(swapped_path.read_bytes(), replacement)
            self.assertTrue(
                displaced.read_bytes().startswith(
                    b"groove-serpent atomic no-replace probe/2\n"
                )
            )
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                sorted((swapped_path.name, displaced.name)),
            )

    def test_rename_no_replace_publishes_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            source = root / "source.tmp"
            destination = root / "destination.json"
            source.write_bytes(b"verified payload")

            rename_no_replace(source, destination)

            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"verified payload")

    def test_rename_no_replace_preserves_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            source = root / "source.tmp"
            destination = root / "destination.json"
            source.write_bytes(b"candidate")
            destination.write_bytes(b"owner")

            with self.assertRaises(FileExistsError):
                rename_no_replace(source, destination)

            self.assertEqual(source.read_bytes(), b"candidate")
            self.assertEqual(destination.read_bytes(), b"owner")

    def test_rename_no_replace_requires_one_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            other = root / "other"
            other.mkdir()
            source = root / "source.tmp"
            source.write_bytes(b"candidate")

            with self.assertRaisesRegex(ValueError, "one parent"):
                rename_no_replace(source, other / "destination.json")

            self.assertTrue(source.exists())

    def test_rename_no_replace_rejects_embedded_nul_without_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            real_source = root / "source"
            destination = root / "destination"
            real_source.write_bytes(b"owner")
            truncated = Path(f"{real_source}\x00ignored")

            with self.assertRaises((ValueError, OSError)):
                rename_no_replace(truncated, destination)

            self.assertEqual(real_source.read_bytes(), b"owner")
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
