from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_continuous_preview_workflow as fixture_module
from groove_serpent import continuous_preview_workflow as continuous


class ContinuousOwnedJsonTests(unittest.TestCase):
    def test_fsync_failure_cleans_known_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with patch.object(continuous.os, "fsync", side_effect=OSError("fsync failure")):
                with self.assertRaisesRegex(OSError, "fsync failure"):
                    continuous._atomic_json(root / "proposal.json", {"test": True})
            self.assertEqual(list(root.iterdir()), [])

    def test_failed_publication_preserves_replaced_temporary(self) -> None:
        for same_bytes in (False, True):
            with self.subTest(same_bytes=same_bytes), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                seen = []

                def replace(source, destination):
                    source = Path(source)
                    payload = source.read_bytes() if same_bytes else b"foreign replacement"
                    source.rename(source.with_suffix(".retained"))
                    source.write_bytes(payload)
                    seen.append((source, payload))
                    raise OSError("publication refusal")

                with patch.object(continuous, "rename_no_replace", side_effect=replace):
                    with self.assertRaisesRegex(OSError, "publication refusal"):
                        continuous._atomic_json(root / "proposal.json", {"test": True})
                self.assertEqual(seen[0][0].read_bytes(), seen[0][1])

    def test_failed_publication_removes_known_temporary_preserves_destination(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            destination = root / "proposal.json"

            def collide(source, target):
                Path(target).write_bytes(b"foreign destination")
                raise FileExistsError("destination collision")

            with patch.object(continuous, "rename_no_replace", side_effect=collide):
                with self.assertRaises(FileExistsError):
                    continuous._atomic_json(destination, {"test": True})
            self.assertEqual(destination.read_bytes(), b"foreign destination")
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_success_does_not_unlink_repopulated_temporary_name(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            original = continuous.rename_no_replace
            seen = []

            def publish_then_repopulate(source, target):
                original(source, target)
                Path(source).write_bytes(b"new occupant")
                seen.append(Path(source))

            with patch.object(continuous, "rename_no_replace", side_effect=publish_then_repopulate):
                continuous._atomic_json(root / "proposal.json", {"test": True})
            self.assertEqual(seen[0].read_bytes(), b"new occupant")
            self.assertTrue((root / "proposal.json").exists())

    def test_cleanup_error_does_not_mask_primary_failure(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            with patch.object(
                continuous, "rename_no_replace", side_effect=OSError("primary failure")
            ), patch.object(
                continuous, "remove_owned_file_if_present", side_effect=OSError("cleanup failure")
            ):
                with self.assertRaisesRegex(OSError, "primary failure") as caught:
                    continuous._atomic_json(Path(name) / "proposal.json", {"test": True})
            self.assertIn("cleanup failure", " ".join(caught.exception.__notes__))


class ContinuousOwnedStageTests(unittest.TestCase):
    def test_stage_cleanup_preserves_replacement_and_unknown_child_directories(self) -> None:
        for mode in ("replace", "unknown-child", "ordinary-failure"):
            with self.subTest(mode=mode):
                fixture = fixture_module.ContinuousPreviewWorkflowTests()
                fixture.setUp()
                try:
                    _, proposal = fixture._propose()
                    original = continuous.rename_no_replace
                    seen = []

                    def refuse_stage(source, target):
                        source = Path(source)
                        if not source.is_dir():
                            return original(source, target)
                        seen.append(source)
                        if mode == "replace":
                            source.rename(source.with_name(source.name + ".retained"))
                            source.mkdir()
                            (source / "foreign.txt").write_bytes(b"foreign directory")
                        elif mode == "unknown-child":
                            (source / "foreign").mkdir()
                            (source / "foreign" / "sentinel").write_bytes(b"foreign directory")
                        raise OSError("stage publication refusal")

                    with patch.object(continuous, "rename_no_replace", side_effect=refuse_stage):
                        with self.assertRaisesRegex(OSError, "stage publication refusal"):
                            continuous.render_continuous_preview(
                                fixture.project, proposal, fixture._attestation(proposal)
                            )
                    stage = seen[0]
                    if mode == "replace":
                        self.assertEqual((stage / "foreign.txt").read_bytes(), b"foreign directory")
                    elif mode == "unknown-child":
                        self.assertEqual(
                            (stage / "foreign" / "sentinel").read_bytes(), b"foreign directory"
                        )
                    else:
                        self.assertFalse(stage.exists())
                    self.assertEqual(
                        fixture_module.sha256_file(fixture.source), fixture.source_sha256
                    )
                finally:
                    fixture.tearDown()
