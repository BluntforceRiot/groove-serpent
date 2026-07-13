from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, BinaryIO
from unittest.mock import patch

from groove_serpent.audio_snapshot import verified_audio_snapshot
from groove_serpent.cache_storage import inspect_snapshot_cache
from groove_serpent.errors import ProjectValidationError


class VerifiedAudioSnapshotTests(unittest.TestCase):
    def test_capture_reads_live_source_once_and_does_not_rehash_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            live = directory / "side.flac"
            payload = b"single pass collector source" * 100_000
            live.write_bytes(payload)
            path_type = type(live)
            original_open = path_type.open
            bytes_read = 0
            snapshot_binary_reads = 0

            class CountingReader:
                def __init__(self, handle: BinaryIO) -> None:
                    self.handle = handle

                def __enter__(self) -> "CountingReader":
                    self.handle.__enter__()
                    return self

                def __exit__(self, *args: object) -> object:
                    return self.handle.__exit__(*args)

                def read(self, size: int = -1) -> bytes:
                    nonlocal bytes_read
                    value = self.handle.read(size)
                    bytes_read += len(value)
                    return value

                def __getattr__(self, name: str) -> Any:
                    return getattr(self.handle, name)

            def observed_open(
                path: Path,
                mode: str = "r",
                buffering: int = -1,
                encoding: str | None = None,
                errors: str | None = None,
                newline: str | None = None,
            ) -> Any:
                nonlocal snapshot_binary_reads
                handle = original_open(
                    path,
                    mode,
                    buffering,
                    encoding,
                    errors,
                    newline,
                )
                if path.name == live.name and mode == "rb":
                    return CountingReader(handle)
                if path.name == "source.flac" and mode == "rb":
                    snapshot_binary_reads += 1
                return handle

            with patch.object(path_type, "open", observed_open):
                snapshot = verified_audio_snapshot(
                    live,
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_size_bytes=len(payload),
                    workspace=directory / "snapshots",
                )
                try:
                    self.assertEqual(snapshot.sha256, hashlib.sha256(payload).hexdigest())
                finally:
                    snapshot.close()

            self.assertEqual(bytes_read, len(payload))
            self.assertEqual(snapshot_binary_reads, 0)

    def test_cheap_evidence_lease_rejects_restored_mtime_snapshot_tamper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            live = directory / "side.flac"
            payload = b"A" * 4_096
            live.write_bytes(payload)
            snapshot = verified_audio_snapshot(
                live,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                expected_size_bytes=len(payload),
                workspace=directory / "snapshots",
            )
            try:
                captured_stat = snapshot.path.stat()
                with snapshot.path.open("r+b") as handle:
                    handle.seek(len(payload) // 2)
                    handle.write(b"B")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.utime(
                    snapshot.path,
                    ns=(captured_stat.st_atime_ns, captured_stat.st_mtime_ns),
                )

                with patch(
                    "groove_serpent.audio_snapshot.assert_file_receipt",
                    side_effect=AssertionError(
                        "a cheap evidence lease must not hash the snapshot"
                    ),
                ), self.assertRaisesRegex(
                    ProjectValidationError,
                    "snapshot lease changed",
                ):
                    snapshot.assert_evidence_lease()
            finally:
                snapshot.close()

    def test_startup_reclaims_hard_exit_snapshot_but_preserves_live_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            live = directory / "side.flac"
            cache = directory / "snapshot-cache"
            payload = b"hard exit capture" * 10_000
            live.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            parent_snapshot = verified_audio_snapshot(
                live,
                expected_sha256=digest,
                expected_size_bytes=len(payload),
                workspace=cache,
            )
            child_script = """
import os
import sys
from pathlib import Path
from groove_serpent.audio_snapshot import verified_audio_snapshot
snapshot = verified_audio_snapshot(
    Path(sys.argv[1]),
    expected_sha256=sys.argv[3],
    expected_size_bytes=int(sys.argv[4]),
    workspace=Path(sys.argv[2]),
)
print(snapshot.path, flush=True)
os._exit(0)
"""
            try:
                child = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        child_script,
                        str(live),
                        str(cache),
                        digest,
                        str(len(payload)),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                child_snapshot_path = Path(child.stdout.strip())
                self.assertTrue(child_snapshot_path.is_file())
                self.assertTrue(parent_snapshot.path.is_file())

                startup_snapshot = verified_audio_snapshot(
                    live,
                    expected_sha256=digest,
                    expected_size_bytes=len(payload),
                    workspace=cache,
                )
                try:
                    self.assertFalse(child_snapshot_path.exists())
                    self.assertTrue(parent_snapshot.path.is_file())
                    self.assertTrue(startup_snapshot.path.is_file())
                    status = inspect_snapshot_cache(cache)
                    self.assertEqual(len(status.entries), 2)
                    self.assertTrue(
                        all(entry.owner_status == "live" for entry in status.entries)
                    )
                finally:
                    startup_snapshot.close()
            finally:
                parent_snapshot.close()

    def test_capture_swap_and_restore_cannot_redirect_stable_handle(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            live = directory / "side.flac"
            parked = directory / "parked-original.flac"
            replacement = directory / "replacement.flac"
            original = b"original audio object" * 100_000
            live.write_bytes(original)
            replacement.write_bytes(b"temporary attacker bytes")
            path_type = type(live)
            original_open = path_type.open
            swapped = False

            class SwappingReader:
                def __init__(self, handle: BinaryIO) -> None:
                    self.handle = handle

                def __enter__(self) -> "SwappingReader":
                    self.handle.__enter__()
                    return self

                def __exit__(self, *args: object) -> object:
                    return self.handle.__exit__(*args)

                def read(self, size: int = -1) -> bytes:
                    nonlocal swapped
                    if swapped:
                        return self.handle.read(size)
                    swapped = True
                    live.replace(parked)
                    try:
                        replacement.replace(live)
                    except BaseException:
                        parked.replace(live)
                        raise
                    try:
                        return self.handle.read(size)
                    finally:
                        live.unlink()
                        parked.replace(live)

                def __getattr__(self, name: str) -> Any:
                    return getattr(self.handle, name)

            def swapping_open(
                path: Path,
                mode: str = "r",
                buffering: int = -1,
                encoding: str | None = None,
                errors: str | None = None,
                newline: str | None = None,
            ) -> Any:
                handle = original_open(
                    path,
                    mode,
                    buffering,
                    encoding,
                    errors,
                    newline,
                )
                if path.name == live.name and mode == "rb":
                    return SwappingReader(handle)
                return handle

            try:
                with patch.object(path_type, "open", swapping_open):
                    snapshot = verified_audio_snapshot(
                        live,
                        expected_sha256=hashlib.sha256(original).hexdigest(),
                        expected_size_bytes=len(original),
                        workspace=directory / "snapshots",
                    )
            except ProjectValidationError:
                self.assertEqual(live.read_bytes(), original)
                return
            try:
                self.assertTrue(swapped)
                self.assertEqual(snapshot.path.read_bytes(), original)
                self.assertEqual(live.read_bytes(), original)
            finally:
                snapshot.close()

    def test_snapshot_is_an_independent_verified_copy_and_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            live = directory / "side.flac"
            payload = b"collector source" * 128
            live.write_bytes(payload)
            snapshot_path: Path

            with verified_audio_snapshot(
                live,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                expected_size_bytes=len(payload),
                workspace=directory / "snapshots",
            ) as snapshot:
                snapshot_path = snapshot.path
                self.assertNotEqual(snapshot.path, live)
                self.assertEqual(snapshot.path.read_bytes(), payload)
                self.assertEqual(snapshot.sha256, hashlib.sha256(payload).hexdigest())

            self.assertFalse(snapshot_path.exists())

    def test_expected_identity_mismatch_is_rejected_before_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            live = Path(directory_value) / "side.flac"
            live.write_bytes(b"source")

            with self.assertRaises(ProjectValidationError):
                verified_audio_snapshot(live, expected_sha256="0" * 64)
            with self.assertRaises(ProjectValidationError):
                verified_audio_snapshot(live, expected_size_bytes=999)

    def test_live_swap_and_restore_cannot_redirect_snapshot_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            live = Path(directory_value) / "side.flac"
            original = b"original audio" * 100
            live.write_bytes(original)

            snapshot_path: Path | None = None
            with self.assertRaisesRegex(ProjectValidationError, "Source audio changed"):
                with verified_audio_snapshot(live) as snapshot:
                    snapshot_path = snapshot.path
                    live.write_bytes(b"temporary replacement")
                    self.assertEqual(snapshot.path.read_bytes(), original)
                    live.write_bytes(original)
            self.assertIsNotNone(snapshot_path)
            assert snapshot_path is not None
            self.assertFalse(snapshot_path.exists())

    def test_persistent_live_change_fails_closed_and_cleans_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            live = Path(directory_value) / "side.flac"
            live.write_bytes(b"original")
            snapshot_path: Path | None = None

            with self.assertRaisesRegex(
                ProjectValidationError, "changed during the verified audio operation"
            ):
                with verified_audio_snapshot(live) as snapshot:
                    snapshot_path = snapshot.path
                    live.write_bytes(b"changed")

            self.assertIsNotNone(snapshot_path)
            assert snapshot_path is not None
            self.assertFalse(snapshot_path.exists())

    def test_snapshot_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            live = Path(directory_value) / "side.flac"
            live.write_bytes(b"original")

            with self.assertRaisesRegex(
                ProjectValidationError, "staged source audio snapshot changed"
            ):
                with verified_audio_snapshot(live) as snapshot:
                    snapshot.path.write_bytes(b"tampered")


if __name__ == "__main__":
    unittest.main()
