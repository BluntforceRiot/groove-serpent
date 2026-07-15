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

import groove_serpent.cache_storage as cache_storage_module
from groove_serpent.cache_storage import (
    CACHE_ENVIRONMENT_VARIABLE,
    LEGACY_SNAPSHOT_LEASE_SCHEMA,
    SNAPSHOT_DIRECTORY_PREFIX,
    SNAPSHOT_LEASE_FILENAME,
    SNAPSHOT_LEASE_SCHEMA,
    SnapshotLeaseMetadata,
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
                self.assertRegex(
                    raw["owner_process_namespace_identity"],
                    r"^local-namespace-sha256:[0-9a-f]{64}$",
                )
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

    def test_legacy_lease_is_unknown_without_querying_its_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "cache"
            lease = acquire_snapshot_lease(
                root,
                source_sha256=self.source_sha256,
                source_size_bytes=64,
            )
            raw = json.loads(lease.receipt_path.read_text(encoding="utf-8"))
            raw["schema"] = LEGACY_SNAPSHOT_LEASE_SCHEMA
            raw.pop("owner_process_namespace_identity")
            lease.receipt_path.write_text(
                json.dumps(raw, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with mock.patch(
                "groove_serpent.cache_storage._pid_exists"
            ) as pid_exists:
                status = inspect_snapshot_cache(root)
                cleaned = cleanup_stale_snapshots(root)

            pid_exists.assert_not_called()
            self.assertEqual(status.entries[0].owner_status, "unknown")
            self.assertFalse(status.entries[0].reclaimable)
            self.assertEqual(cleaned.removed, ())
            self.assertEqual(cleaned.skipped_unknown, 1)
            parsed = SnapshotLeaseMetadata.from_dict(raw)
            self.assertEqual(set(parsed.to_dict()), set(raw))
            self.assertNotIn(
                "owner_process_namespace_identity", parsed.to_dict()
            )
            lease._released = True

    def test_foreign_process_namespace_is_unknown_before_pid_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "cache"
            lease = acquire_snapshot_lease(
                root,
                source_sha256=self.source_sha256,
                source_size_bytes=64,
            )
            raw = json.loads(lease.receipt_path.read_text(encoding="utf-8"))
            raw["owner_process_namespace_identity"] = (
                "local-namespace-sha256:" + "f" * 64
            )
            lease.receipt_path.write_text(
                json.dumps(raw, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with mock.patch(
                "groove_serpent.cache_storage._pid_exists"
            ) as pid_exists:
                status = inspect_snapshot_cache(root)
                cleaned = cleanup_stale_snapshots(root)

            pid_exists.assert_not_called()
            self.assertEqual(status.entries[0].owner_status, "unknown")
            self.assertEqual(cleaned.removed, ())
            self.assertTrue(lease.directory.exists())
            lease._released = True

    def test_windows_machine_guid_is_unavailable_off_windows(self) -> None:
        with mock.patch.object(cache_storage_module.sys, "platform", "linux"):
            self.assertIsNone(cache_storage_module._windows_machine_guid())

    def test_cloned_machine_guid_is_separated_by_boot_session(self) -> None:
        with mock.patch.object(
            cache_storage_module,
            "_windows_machine_guid",
            return_value="cloned-machine-guid",
        ), mock.patch.object(
            cache_storage_module,
            "_windows_boot_session_identity",
            return_value="boot-filetime:100;session:1",
        ):
            first = cache_storage_module._process_namespace_identity_windows()
        with mock.patch.object(
            cache_storage_module,
            "_windows_machine_guid",
            return_value="cloned-machine-guid",
        ), mock.patch.object(
            cache_storage_module,
            "_windows_boot_session_identity",
            return_value="boot-filetime:200;session:1",
        ):
            second = cache_storage_module._process_namespace_identity_windows()
        self.assertRegex(first or "", r"^local-namespace-sha256:[0-9a-f]{64}$")
        self.assertRegex(second or "", r"^local-namespace-sha256:[0-9a-f]{64}$")
        self.assertNotEqual(first, second)

    def test_malformed_quarantine_name_never_grants_cleanup_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "cache"
            lease = acquire_snapshot_lease(
                root,
                source_sha256=self.source_sha256,
                source_size_bytes=64,
            )
            malformed = root / ".groove-serpent-quarantine-garbage.preserved"
            os.replace(lease.directory, malformed)

            with mock.patch(
                "groove_serpent.cache_storage._pid_exists", return_value=False
            ):
                status = inspect_snapshot_cache(root)
                cleaned = cleanup_stale_snapshots(root)

            self.assertEqual(len(status.entries), 1)
            self.assertEqual(status.entries[0].directory.name, malformed.name)
            self.assertIsNone(status.entries[0].metadata)
            self.assertFalse(status.entries[0].reclaimable)
            self.assertEqual(cleaned.removed, ())
            self.assertEqual(cleaned.skipped_unknown, 1)
            self.assertTrue(malformed.exists())
            lease._released = True

    def test_assert_owned_rejects_a_process_namespace_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "cache"
            lease = acquire_snapshot_lease(
                root,
                source_sha256=self.source_sha256,
                source_size_bytes=64,
            )
            with mock.patch(
                "groove_serpent.cache_storage.process_namespace_identity",
                return_value="local-namespace-sha256:" + "e" * 64,
            ), self.assertRaisesRegex(GrooveSerpentError, "namespace changed"):
                lease.assert_owned()
            lease.release()

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

            self.assertEqual(exists.call_count, 3)
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

            self.assertEqual(exists.call_count, 3)
            self.assertEqual(identity.call_count, 3)
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

    def test_hardlinked_receipt_is_not_trusted_as_cache_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            root = directory / "cache"
            lease = acquire_snapshot_lease(
                root,
                source_sha256=self.source_sha256,
                source_size_bytes=64,
            )
            alias = directory / "receipt-alias.json"
            try:
                alias.hardlink_to(lease.receipt_path)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"Hardlink creation is unavailable: {exc}")

            status = inspect_snapshot_cache(root)
            self.assertEqual(len(status.entries), 1)
            entry = status.entries[0]
            self.assertIsNone(entry.metadata)
            self.assertFalse(entry.reclaimable)
            self.assertIn("multi-link", entry.problem or "")
            with mock.patch(
                "groove_serpent.cache_storage._pid_exists", return_value=False
            ):
                cleaned = cleanup_stale_snapshots(root)
            self.assertEqual(cleaned.removed, ())
            self.assertEqual(cleaned.skipped_unknown, 1)
            self.assertTrue(lease.directory.exists())
            self.assertEqual(alias.read_bytes(), lease.receipt_path.read_bytes())

    def test_symlinked_receipt_is_not_trusted_or_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            root = directory / "cache"
            lease = acquire_snapshot_lease(
                root,
                source_sha256=self.source_sha256,
                source_size_bytes=64,
            )
            outside = directory / "outside-receipt.json"
            outside.write_bytes(lease.receipt_path.read_bytes())
            lease.receipt_path.unlink()
            try:
                lease.receipt_path.symlink_to(outside)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"File symlinks are unavailable under this policy: {exc}")

            status = inspect_snapshot_cache(root)
            self.assertEqual(len(status.entries), 1)
            self.assertIsNone(status.entries[0].metadata)
            self.assertFalse(status.entries[0].reclaimable)
            self.assertIn("symbolic-link", status.entries[0].problem or "")
            with mock.patch(
                "groove_serpent.cache_storage._pid_exists", return_value=False
            ):
                cleaned = cleanup_stale_snapshots(root)
            self.assertEqual(cleaned.removed, ())
            self.assertTrue(lease.directory.exists())
            self.assertEqual(outside.read_bytes(), lease.receipt_path.read_bytes())

    def test_linked_cache_member_retains_otherwise_stale_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            root = directory / "cache"
            lease = acquire_snapshot_lease(
                root,
                source_sha256=self.source_sha256,
                source_size_bytes=64,
            )
            outside = directory / "outside.flac"
            outside.write_bytes(b"outside audio")
            linked = lease.directory / "linked-source.flac"
            try:
                linked.hardlink_to(outside)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"Hardlink creation is unavailable: {exc}")

            with mock.patch(
                "groove_serpent.cache_storage._pid_exists", return_value=False
            ):
                status = inspect_snapshot_cache(root)
                cleaned = cleanup_stale_snapshots(root)
            self.assertIsNone(status.entries[0].metadata)
            self.assertIn("multi-link", status.entries[0].problem or "")
            self.assertEqual(cleaned.removed, ())
            self.assertTrue(lease.directory.exists())
            self.assertEqual(outside.read_bytes(), b"outside audio")

    def test_cleanup_rechecks_for_link_inserted_after_initial_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            root = directory / "cache"
            lease = acquire_snapshot_lease(
                root,
                source_sha256=self.source_sha256,
                source_size_bytes=64,
            )
            outside = directory / "outside.flac"
            outside.write_bytes(b"outside audio")
            linked = lease.directory / "late-linked-source.flac"
            real_load = cache_storage_module._load_metadata
            calls = 0

            def load_then_insert(path: Path):
                nonlocal calls
                metadata = real_load(path)
                calls += 1
                if calls == 2:
                    linked.hardlink_to(outside)
                return metadata

            with mock.patch(
                "groove_serpent.cache_storage._pid_exists", return_value=False
            ), mock.patch(
                "groove_serpent.cache_storage._load_metadata",
                side_effect=load_then_insert,
            ):
                cleaned = cleanup_stale_snapshots(root)

            self.assertEqual(calls, 2)
            self.assertEqual(cleaned.removed, ())
            self.assertEqual(cleaned.skipped_unknown, 1)
            self.assertTrue(lease.directory.exists())
            self.assertEqual(outside.read_bytes(), b"outside audio")

    def test_cleanup_quarantines_a_swapped_live_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "cache"
            stale = acquire_snapshot_lease(
                root,
                source_sha256="a" * 64,
                source_size_bytes=5,
            )
            live = acquire_snapshot_lease(
                root,
                source_sha256="b" * 64,
                source_size_bytes=4,
            )
            stale_payload = b"stale"
            live_payload = b"live"
            (stale.directory / "stale.marker").write_bytes(stale_payload)
            (live.directory / "live.marker").write_bytes(live_payload)
            raw = json.loads(stale.receipt_path.read_text(encoding="utf-8"))
            raw["owner_pid"] = 2_000_000_000
            raw["owner_process_creation_identity"] = "dead-process"
            stale.receipt_path.write_text(
                json.dumps(raw, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            held_stale = root / f"{SNAPSHOT_DIRECTORY_PREFIX}held-stale"
            real_quarantine = cache_storage_module.quarantine_path_no_replace
            swapped = False

            def quarantine_after_swap(path: Path, *, purpose: str) -> Path:
                nonlocal swapped
                if path == stale.directory and not swapped:
                    os.replace(stale.directory, held_stale)
                    os.replace(live.directory, stale.directory)
                    swapped = True
                return real_quarantine(path, purpose=purpose)

            with mock.patch.object(
                cache_storage_module,
                "quarantine_path_no_replace",
                side_effect=quarantine_after_swap,
            ), mock.patch(
                "groove_serpent.cache_storage._pid_exists",
                side_effect=lambda pid: pid != 2_000_000_000,
            ):
                cleaned = cleanup_stale_snapshots(root)

            self.assertTrue(swapped)
            self.assertEqual(cleaned.removed, ())
            self.assertEqual(cleaned.skipped_unknown, 1)
            self.assertEqual((held_stale / "stale.marker").read_bytes(), stale_payload)
            live_markers = list(root.rglob("live.marker"))
            self.assertEqual(len(live_markers), 1)
            self.assertEqual(live_markers[0].read_bytes(), live_payload)
            stale._released = True
            live._released = True

    def test_release_quarantines_a_swapped_live_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "cache"
            releasing = acquire_snapshot_lease(
                root,
                source_sha256="c" * 64,
                source_size_bytes=7,
            )
            victim = acquire_snapshot_lease(
                root,
                source_sha256="d" * 64,
                source_size_bytes=6,
            )
            releasing_payload = b"releasing"
            victim_payload = b"victim"
            (releasing.directory / "release.marker").write_bytes(
                releasing_payload
            )
            (victim.directory / "victim.marker").write_bytes(victim_payload)
            held_release = root / f"{SNAPSHOT_DIRECTORY_PREFIX}held-release"
            real_quarantine = cache_storage_module.quarantine_path_no_replace
            swapped = False

            def quarantine_after_swap(path: Path, *, purpose: str) -> Path:
                nonlocal swapped
                if path == releasing.directory and not swapped:
                    os.replace(releasing.directory, held_release)
                    os.replace(victim.directory, releasing.directory)
                    swapped = True
                return real_quarantine(path, purpose=purpose)

            with mock.patch.object(
                cache_storage_module,
                "quarantine_path_no_replace",
                side_effect=quarantine_after_swap,
            ), self.assertRaisesRegex(GrooveSerpentError, "unowned directory"):
                releasing.release()

            self.assertTrue(swapped)
            self.assertEqual(
                (held_release / "release.marker").read_bytes(), releasing_payload
            )
            victim_markers = list(root.rglob("victim.marker"))
            self.assertEqual(len(victim_markers), 1)
            self.assertEqual(victim_markers[0].read_bytes(), victim_payload)
            releasing._released = True
            victim._released = True

    def test_out_of_range_owner_pid_is_retained_as_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "cache"
            lease = acquire_snapshot_lease(
                root,
                source_sha256=self.source_sha256,
                source_size_bytes=1,
            )
            raw = json.loads(lease.receipt_path.read_text(encoding="utf-8"))
            raw["owner_pid"] = 10**100
            lease.receipt_path.write_text(
                json.dumps(raw, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            status = inspect_snapshot_cache(root)
            self.assertEqual(len(status.entries), 1)
            self.assertIsNone(status.entries[0].metadata)
            self.assertFalse(status.entries[0].reclaimable)
            self.assertIn("supported range", status.entries[0].problem or "")
            cleaned = cleanup_stale_snapshots(root)
            self.assertEqual(cleaned.removed, ())
            self.assertEqual(cleaned.skipped_unknown, 1)
            self.assertTrue(lease.directory.exists())
            lease._released = True

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

    def test_interrupted_release_quarantine_remains_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "cache"
            lease = acquire_snapshot_lease(
                root,
                source_sha256=self.source_sha256,
                source_size_bytes=64,
            )
            (lease.directory / "source.flac").write_bytes(b"audio")
            with mock.patch(
                "groove_serpent.cache_storage._destroy_owned_quarantine",
                side_effect=KeyboardInterrupt,
            ), self.assertRaises(KeyboardInterrupt):
                lease.release()

            preserved = [
                item
                for item in root.iterdir()
                if item.name.endswith(".preserved")
            ]
            self.assertEqual(len(preserved), 1)
            self.assertTrue(
                (preserved[0] / SNAPSHOT_LEASE_FILENAME).is_file()
            )
            with mock.patch(
                "groove_serpent.cache_storage._pid_exists", return_value=False
            ):
                status = inspect_snapshot_cache(root)
                cleaned = cleanup_stale_snapshots(root)
            self.assertEqual(len(status.entries), 1)
            self.assertEqual(
                status.entries[0].directory.name, preserved[0].name
            )
            self.assertTrue(status.entries[0].reclaimable)
            self.assertEqual(
                tuple(item.name for item in cleaned.removed),
                (preserved[0].name,),
            )
            self.assertFalse(preserved[0].exists())
            lease._released = True

    def test_partial_stale_cleanup_keeps_receipt_for_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "cache"
            lease = acquire_snapshot_lease(
                root,
                source_sha256=self.source_sha256,
                source_size_bytes=64,
            )
            source = lease.directory / "source.flac"
            source.write_bytes(b"audio")
            real_unlink = Path.unlink
            interrupted = False

            def unlink_then_interrupt(
                path: Path, missing_ok: bool = False
            ) -> None:
                nonlocal interrupted
                real_unlink(path, missing_ok=missing_ok)
                if path.name == "source.flac" and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt

            with mock.patch(
                "groove_serpent.cache_storage._pid_exists", return_value=False
            ), mock.patch.object(
                Path, "unlink", autospec=True, side_effect=unlink_then_interrupt
            ), self.assertRaises(KeyboardInterrupt):
                cleanup_stale_snapshots(root)

            preserved = [
                item
                for item in root.iterdir()
                if item.name.endswith(".preserved")
            ]
            self.assertTrue(interrupted)
            self.assertEqual(len(preserved), 1)
            self.assertFalse((preserved[0] / "source.flac").exists())
            self.assertTrue(
                (preserved[0] / SNAPSHOT_LEASE_FILENAME).is_file()
            )
            with mock.patch(
                "groove_serpent.cache_storage._pid_exists", return_value=False
            ):
                status = inspect_snapshot_cache(root)
                cleaned = cleanup_stale_snapshots(root)
            self.assertEqual(len(status.entries), 1)
            self.assertTrue(status.entries[0].reclaimable)
            self.assertEqual(
                tuple(item.name for item in cleaned.removed),
                (preserved[0].name,),
            )
            lease._released = True

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

    @unittest.skipUnless(os.name == "nt", "Windows remote-path semantics")
    def test_remote_directory_identity_ignores_unstable_birth_time(self) -> None:
        first = SimpleNamespace(
            st_dev=7,
            st_ino=11,
            st_mode=0o040700,
            st_file_attributes=0,
            st_birthtime_ns=100,
        )
        renamed = SimpleNamespace(
            st_dev=7,
            st_ino=11,
            st_mode=0o040700,
            st_file_attributes=0,
            st_birthtime_ns=200,
        )
        remote = Path(r"\\server\share\cache")
        self.assertTrue(cache_storage_module._windows_remote_path(remote))
        before = cache_storage_module._DirectoryIdentity.capture(
            first,  # type: ignore[arg-type]
            include_birth=not cache_storage_module._windows_remote_path(remote),
        )
        after = cache_storage_module._DirectoryIdentity.capture(
            renamed,  # type: ignore[arg-type]
            include_birth=not cache_storage_module._windows_remote_path(remote),
        )
        self.assertEqual(before, after)
        self.assertIsNone(before.birth_ns)

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
