from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from groove_serpent.cache_storage import (
    CACHE_ENVIRONMENT_VARIABLE,
    SNAPSHOT_DIRECTORY_PREFIX,
    SNAPSHOT_LEASE_FILENAME,
    SNAPSHOT_LEASE_SCHEMA,
    acquire_provisional_snapshot_lease,
    acquire_snapshot_lease,
    cleanup_stale_snapshots,
    ensure_free_space,
    inspect_snapshot_cache,
    resolve_cache_root,
)
from groove_serpent.errors import GrooveSerpentError
from groove_serpent.errors import ProjectValidationError
from groove_serpent.audio_snapshot import verified_audio_snapshot


class CacheStorageTests(unittest.TestCase):
    source_sha256 = "a" * 64

    def test_provisional_lease_is_atomically_bound_after_single_pass_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "cache"
            lease = acquire_provisional_snapshot_lease(
                root,
                source_size_bytes=321,
            )
            try:
                raw = json.loads(lease.receipt_path.read_text(encoding="utf-8"))
                self.assertEqual(raw["lease_state"], "capturing")
                self.assertIsNone(raw["source_sha256"])
                lease.bind_source_identity(self.source_sha256, 321)
                bound = json.loads(
                    lease.receipt_path.read_text(encoding="utf-8")
                )
                self.assertEqual(bound["lease_state"], "active")
                self.assertEqual(bound["source_sha256"], self.source_sha256)
                lease.assert_owned()
                with self.assertRaisesRegex(GrooveSerpentError, "capturing"):
                    lease.bind_source_identity("b" * 64, 321)
            finally:
                lease.release()

    def test_active_lease_records_identity_and_is_not_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "cache"
            lease = acquire_snapshot_lease(
                root,
                source_sha256=self.source_sha256,
                source_size_bytes=123_456,
            )
            try:
                (lease.directory / "source.flac").write_bytes(b"audio")
                raw = json.loads(lease.receipt_path.read_text(encoding="utf-8"))
                self.assertEqual(raw["schema"], SNAPSHOT_LEASE_SCHEMA)
                self.assertEqual(raw["source_sha256"], self.source_sha256)
                self.assertEqual(raw["source_size_bytes"], 123_456)
                self.assertEqual(raw["owner_pid"], os.getpid())
                self.assertIn("owner_process_creation_identity", raw)
                self.assertEqual(raw["lease_state"], "active")
                self.assertTrue(raw["created_at"])
                self.assertTrue(raw["app_version"])

                status = inspect_snapshot_cache(root)
                self.assertEqual(len(status.entries), 1)
                self.assertEqual(status.entries[0].owner_status, "live")
                self.assertFalse(status.entries[0].reclaimable)

                cleaned = cleanup_stale_snapshots(root)
                self.assertEqual(cleaned.removed, ())
                self.assertEqual(cleaned.skipped_live, 1)
                self.assertTrue(lease.directory.exists())
                lease.assert_owned()
            finally:
                lease.release()
            self.assertFalse(lease.directory.exists())

    def test_cleanup_rechecks_owner_and_removes_only_proven_stale_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "cache"
            lease = acquire_snapshot_lease(
                root,
                source_sha256=self.source_sha256,
                source_size_bytes=64,
            )
            (lease.directory / "source.flac").write_bytes(b"x" * 64)
            with mock.patch(
                "groove_serpent.cache_storage._pid_exists", return_value=False
            ) as exists:
                cleaned = cleanup_stale_snapshots(root)

            self.assertEqual(exists.call_count, 2)
            self.assertEqual(cleaned.removed, (lease.directory,))
            self.assertGreaterEqual(cleaned.removed_bytes, 64)
            self.assertFalse(lease.directory.exists())
            # Cleanup remains idempotent even though the normal process object
            # still exists after a simulated hard-termination recovery.
            lease.release()

    def test_pid_reuse_is_reclaimable_and_rechecked_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "cache"
            lease = acquire_snapshot_lease(
                root,
                source_sha256=self.source_sha256,
                source_size_bytes=64,
            )
            raw = json.loads(lease.receipt_path.read_text(encoding="utf-8"))
            raw["owner_process_creation_identity"] = "recorded-prior-process"
            lease.receipt_path.write_text(
                json.dumps(raw, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with mock.patch(
                "groove_serpent.cache_storage._pid_exists", return_value=True
            ), mock.patch(
                "groove_serpent.cache_storage.process_creation_identity",
                return_value="current-reused-process",
            ):
                status = inspect_snapshot_cache(root)

            self.assertEqual(len(status.entries), 1)
            self.assertIsNotNone(status.entries[0].metadata)
            self.assertEqual(status.entries[0].owner_status, "reused")
            self.assertTrue(status.entries[0].reclaimable)

            with mock.patch(
                "groove_serpent.cache_storage._pid_exists", return_value=True
            ) as exists, mock.patch(
                "groove_serpent.cache_storage.process_creation_identity",
                return_value="current-reused-process",
            ) as identity:
                cleaned = cleanup_stale_snapshots(root)

            self.assertEqual(exists.call_count, 2)
            self.assertEqual(identity.call_count, 2)
            self.assertEqual(cleaned.removed, (lease.directory,))
            self.assertFalse(lease.directory.exists())
            lease.release()

    def test_unsafe_prefixed_file_and_link_are_never_followed_or_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            root = directory / "cache"
            root.mkdir()
            unsafe_file = root / f"{SNAPSHOT_DIRECTORY_PREFIX}regular-file"
            unsafe_file.write_bytes(b"keep this file")

            status = inspect_snapshot_cache(root)
            self.assertEqual(len(status.entries), 1)
            self.assertEqual(status.entries[0].directory.name, unsafe_file.name)
            self.assertEqual(status.entries[0].owner_status, "unknown")
            self.assertFalse(status.entries[0].reclaimable)
            self.assertEqual(status.entries[0].bytes_on_disk, 0)

            cleaned = cleanup_stale_snapshots(root)
            self.assertEqual(cleaned.removed, ())
            self.assertEqual(cleaned.skipped_unknown, 1)
            self.assertEqual(unsafe_file.read_bytes(), b"keep this file")

            with self.subTest(entry="directory symlink"):
                outside = directory / "outside-cache"
                outside.mkdir()
                marker = outside / "must-not-be-followed.txt"
                marker.write_bytes(b"outside data")
                unsafe_link = root / f"{SNAPSHOT_DIRECTORY_PREFIX}directory-link"
                try:
                    unsafe_link.symlink_to(outside, target_is_directory=True)
                except (NotImplementedError, OSError) as exc:
                    self.skipTest(
                        f"Directory symlinks are unavailable under this OS policy: {exc}"
                    )

                linked_status = inspect_snapshot_cache(root)
                by_name = {
                    entry.directory.name: entry for entry in linked_status.entries
                }
                self.assertEqual(set(by_name), {unsafe_file.name, unsafe_link.name})
                self.assertEqual(by_name[unsafe_link.name].owner_status, "unknown")
                self.assertFalse(by_name[unsafe_link.name].reclaimable)
                self.assertEqual(by_name[unsafe_link.name].bytes_on_disk, 0)

                linked_cleanup = cleanup_stale_snapshots(root)
                self.assertEqual(linked_cleanup.removed, ())
                self.assertEqual(linked_cleanup.skipped_unknown, 2)
                self.assertTrue(unsafe_link.is_symlink())
                self.assertEqual(marker.read_bytes(), b"outside data")
                self.assertEqual(unsafe_file.read_bytes(), b"keep this file")

    def test_unknown_or_malformed_cache_entries_are_never_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "cache"
            malformed = root / f"{SNAPSHOT_DIRECTORY_PREFIX}malformed"
            malformed.mkdir(parents=True)
            (malformed / SNAPSHOT_LEASE_FILENAME).write_text(
                '{"schema": "not-groove-serpent"}\n',
                encoding="utf-8",
            )
            (malformed / "source.flac").write_bytes(b"keep")

            status = inspect_snapshot_cache(root)
            self.assertEqual(len(status.entries), 1)
            self.assertEqual(status.entries[0].owner_status, "unknown")
            self.assertFalse(status.entries[0].reclaimable)
            cleaned = cleanup_stale_snapshots(root)
            self.assertEqual(cleaned.removed, ())
            self.assertEqual(cleaned.skipped_unknown, 1)
            self.assertTrue(malformed.exists())

    def test_hard_termination_is_reclaimed_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            root = directory / "cache"
            marker = directory / "child-lease.txt"
            code = "\n".join(
                [
                    "import os, sys",
                    "from pathlib import Path",
                    "from groove_serpent.cache_storage import acquire_snapshot_lease",
                    "root, marker = Path(sys.argv[1]), Path(sys.argv[2])",
                    "lease = acquire_snapshot_lease(",
                    "    root, source_sha256='b' * 64, source_size_bytes=1024)",
                    "(lease.directory / 'source.flac').write_bytes(b'x' * 1024)",
                    "with marker.open('w', encoding='utf-8') as handle:",
                    "    handle.write(str(lease.directory))",
                    "    handle.flush()",
                    "    os.fsync(handle.fileno())",
                    "os._exit(17)",
                ]
            )
            completed = subprocess.run(
                [sys.executable, "-c", code, str(root), str(marker)],
                check=False,
            )
            self.assertEqual(completed.returncode, 17)
            abandoned = Path(marker.read_text(encoding="utf-8"))
            self.assertTrue(abandoned.exists())

            status = inspect_snapshot_cache(root)
            self.assertEqual(len(status.entries), 1)
            self.assertIn(status.entries[0].owner_status, {"dead", "reused"})
            self.assertTrue(status.entries[0].reclaimable)
            cleaned = cleanup_stale_snapshots(root)
            self.assertEqual(cleaned.removed, (abandoned,))
            self.assertFalse(abandoned.exists())

    def test_configured_and_project_local_cache_roots_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            project = directory / "side.groove.json"
            expected = directory / ".groove-serpent" / "cache" / "snapshots"
            self.assertEqual(resolve_cache_root(project), expected.resolve())

            configured = directory / "configured-cache"
            with mock.patch.dict(
                os.environ,
                {CACHE_ENVIRONMENT_VARIABLE: str(configured)},
            ):
                self.assertEqual(resolve_cache_root(project), configured.resolve())
                explicit = directory / "explicit"
                self.assertEqual(
                    resolve_cache_root(project, explicit), explicit.resolve()
                )

    def test_storage_preflight_reports_required_and_available_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            destination = Path(directory_value) / "future" / "bundle"
            with mock.patch(
                "groove_serpent.cache_storage.shutil.disk_usage",
                return_value=SimpleNamespace(free=10_000),
            ):
                result = ensure_free_space(
                    destination,
                    8_000,
                    reserve_bytes=1_000,
                    label="Restoration render",
                )
                self.assertEqual(result.required_bytes, 8_000)
                self.assertEqual(result.available_bytes, 10_000)
                with self.assertRaisesRegex(
                    GrooveSerpentError,
                    "requires 9000 bytes.*only 10000 bytes are available",
                ):
                    ensure_free_space(
                        destination,
                        9_000,
                        reserve_bytes=2_000,
                        label="Restoration render",
                    )

    def test_snapshot_preflight_preserves_required_and_available_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source = directory / "source.flac"
            source.write_bytes(b"audio")
            cache = directory / "cache"
            with mock.patch(
                "groove_serpent.audio_snapshot.ensure_free_space",
                side_effect=GrooveSerpentError(
                    "Verified audio snapshot requires 5 bytes plus 64 bytes of reserve, "
                    "but only 4 bytes are available."
                ),
            ), self.assertRaisesRegex(
                ProjectValidationError,
                "requires 5 bytes.*only 4 bytes are available",
            ):
                verified_audio_snapshot(source, workspace=cache)

            self.assertEqual(inspect_snapshot_cache(cache).entries, ())

    def test_invalid_storage_numbers_fail_with_stable_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            destination = Path(directory_value)
            for value in (True, -1):
                with self.subTest(value=value), self.assertRaises(
                    GrooveSerpentError
                ):
                    ensure_free_space(
                        destination,
                        value,  # type: ignore[arg-type]
                        reserve_bytes=0,
                        label="Snapshot",
                    )


if __name__ == "__main__":
    unittest.main()
