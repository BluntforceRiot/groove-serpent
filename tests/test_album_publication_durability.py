from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

import groove_serpent.album_publication_durability as durability_module

from groove_serpent.album import (
    AlbumArtwork,
    AlbumProject,
    AlbumSide,
    pin_album_side,
    save_album_project,
)
from groove_serpent.album_publication_builder import build_album_publication_plan
from groove_serpent.album_publication_durability import (
    inventory_album_publication_orphans,
    recover_album_publication_orphan,
    replay_album_publication,
    verify_album_publication,
)
from groove_serpent.album_publication_executor import execute_album_publication_plan
from groove_serpent.errors import ExportError
from groove_serpent.media import probe_audio
from groove_serpent.models import AnalysisSettings, AnalysisSummary, Project, Track
from groove_serpent.project_io import save_project


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk/wcAAusB9Wl2nWQAAAAASUVORK5CYII="
)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and ffprobe are required",
)
class AlbumPublicationDurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _fixture(
        self,
        profiles: tuple[str, ...],
        *,
        artwork: bool = False,
        second_side: str | None = None,
    ) -> tuple[Path, Path]:
        if second_side not in {None, "shared", "distinct"}:
            raise AssertionError("Unsupported second-side fixture mode.")
        source_path = self.root / "side-a.flac"
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=997:sample_rate=48000:duration=0.4",
                "-ac",
                "2",
                "-c:a",
                "flac",
                "-sample_fmt",
                "s16",
                str(source_path),
            ],
            check=True,
        )
        source = probe_audio(source_path, stored_path=source_path.name)
        project = Project(
            source=source,
            settings=AnalysisSettings(min_track_seconds=0.05),
            analysis=AnalysisSummary(
                music_start_seconds=0.0,
                music_end_seconds=0.4,
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=[
                Track(1, "First", 0, 9_600, 0.0, 0.2),
                Track(2, "Second", 9_600, 19_200, 0.2, 0.4),
            ],
            metadata={"artist": "Side Artist", "album": "Side Album"},
        )
        project_path = self.root / "side-a.groove.json"
        save_project(project, project_path)
        album_path = self.root / "album.groove-album.json"
        side = AlbumSide("A", 1, project_path.name)
        pin_album_side(side, album_path)
        sides = [side]
        if second_side is not None:
            if second_side == "shared":
                second_source = source
            else:
                second_source_path = self.root / "side-b.flac"
                subprocess.run(
                    [
                        shutil.which("ffmpeg") or "ffmpeg",
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=431:sample_rate=48000:duration=0.4",
                        "-ac",
                        "2",
                        "-c:a",
                        "flac",
                        "-sample_fmt",
                        "s16",
                        str(second_source_path),
                    ],
                    check=True,
                )
                second_source = probe_audio(
                    second_source_path,
                    stored_path=second_source_path.name,
                )
            second_tracks = [Track.from_dict(asdict(track)) for track in project.tracks]
            for track in second_tracks:
                track.title = f"B {track.title}"
                track.side = "B"
            second_project_path = self.root / "side-b.groove.json"
            save_project(
                Project(
                    source=second_source,
                    settings=project.settings,
                    analysis=project.analysis,
                    tracks=second_tracks,
                    metadata={**project.metadata, "side": "B"},
                ),
                second_project_path,
            )
            second = AlbumSide("B", 2, second_project_path.name)
            pin_album_side(second, album_path)
            sides.append(second)
        album_artwork: AlbumArtwork | None = None
        if artwork:
            artwork_path = self.root / "cover.png"
            artwork_path.write_bytes(_PNG_1X1)
            album_artwork = AlbumArtwork(
                artwork_path.name,
                hashlib.sha256(_PNG_1X1).hexdigest(),
            )
        save_album_project(
            AlbumProject(
                metadata={"artist": "Album Artist", "album": "Test Album"},
                sides=sides,
                artwork=album_artwork,
            ),
            album_path,
        )
        plan_path = self.root / "publication-plan.json"
        build_album_publication_plan(
            album_path,
            plan_path,
            selected_profiles=profiles,
            restoration_mode="none",
        )
        return plan_path, source_path

    @staticmethod
    def _tree_receipts(root: Path) -> dict[str, tuple[int, int, int, str]]:
        receipts: dict[str, tuple[int, int, int, str]] = {}
        for path in sorted(root.rglob("*")):
            if path.is_file():
                metadata = path.stat()
                receipts[path.relative_to(root).as_posix()] = (
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
        return receipts

    @staticmethod
    def _refresh_audio_inventory(
        output: Path,
        target: Path,
    ) -> None:
        manifest_path = output / "groove-serpent-album-publication.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        relative = target.relative_to(output).as_posix()
        item = next(value for value in manifest["inventory"] if value["path"] == relative)
        raw = target.read_bytes()
        item["size_bytes"] = len(raw)
        item["sha256"] = hashlib.sha256(raw).hexdigest()
        item["verification"]["audio_attestation"] = durability_module._audio_attestation(target)
        manifest_path.write_text(
            json.dumps(
                manifest,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_verify_real_publication_is_read_only_and_checks_audio_attestation(
        self,
    ) -> None:
        plan_path, _source = self._fixture(
            ("corrected-lossless", "portable"),
            artwork=True,
        )
        output = self.root / "published"
        execute_album_publication_plan(plan_path, output)
        before = self._tree_receipts(output)

        report = verify_album_publication(output)

        self.assertTrue(report.ok, report.mismatches)
        self.assertGreater(report.artifact_count, 0)
        self.assertEqual(before, self._tree_receipts(output))
        manifest = json.loads(
            (output / "groove-serpent-album-publication.json").read_text(encoding="utf-8")
        )
        audio = [item for item in manifest["inventory"] if item["path"].endswith((".flac", ".m4a"))]
        self.assertTrue(audio)
        self.assertTrue(
            all(
                item["verification"]["audio_attestation"]["complete_decode_verified"]
                for item in audio
            )
        )
        self.assertTrue(
            all(
                item["verification"]["audio_attestation"]["embedded_artwork_sha256"]
                == hashlib.sha256(_PNG_1X1).hexdigest()
                for item in audio
            )
        )

    def test_duplicate_key_manifest_is_rejected(self) -> None:
        plan_path, _source = self._fixture(("archival-source",))
        output = self.root / "published"
        execute_album_publication_plan(plan_path, output)
        manifest_path = output / "groove-serpent-album-publication.json"
        text = manifest_path.read_text(encoding="utf-8")
        manifest_path.write_text(
            text.replace(
                '"schema":',
                '"schema": "groove-serpent.album-publication-manifest/1",\n  "schema":',
                1,
            ),
            encoding="utf-8",
        )

        report = verify_album_publication(output)

        self.assertFalse(report.ok)
        self.assertIn("Duplicate JSON key", report.mismatches[0].message)

    def test_portable_equivalent_inventory_paths_are_rejected(self) -> None:
        plan_path, _source = self._fixture(("archival-source",))
        output = self.root / "published"
        execute_album_publication_plan(plan_path, output)
        manifest_path = output / "groove-serpent-album-publication.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        duplicate = dict(manifest["inventory"][0])
        duplicate["path"] = duplicate["path"].upper()
        manifest["inventory"].append(duplicate)
        manifest["inventory"].sort(key=lambda item: item["path"])
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        report = verify_album_publication(output)

        self.assertFalse(report.ok)
        self.assertIn("duplicate portable paths", report.mismatches[0].message)

    def test_audio_byte_tamper_is_rejected_before_it_can_be_blessed(self) -> None:
        plan_path, _source = self._fixture(("corrected-lossless",))
        output = self.root / "published"
        execute_album_publication_plan(plan_path, output)
        target = next((output / "corrected-lossless").glob("*.flac"))
        target.write_bytes(target.read_bytes() + b"tamper")

        report = verify_album_publication(output)

        self.assertFalse(report.ok)
        self.assertIn("differs from inventory", report.mismatches[0].message)

    def test_coherent_audio_and_manifest_rewrite_cannot_bless_wrong_tags(self) -> None:
        plan_path, _source = self._fixture(("corrected-lossless",), artwork=True)
        output = self.root / "published"
        execute_album_publication_plan(plan_path, output)
        target = next((output / "corrected-lossless").glob("*.flac"))
        replacement = target.with_name(f"{target.stem}-rewrite.flac")
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(target),
                "-map",
                "0",
                "-c",
                "copy",
                "-metadata",
                "title=Wrong Title",
                str(replacement),
            ],
            check=True,
        )
        target.unlink()
        replacement.rename(target)
        self._refresh_audio_inventory(output, target)

        report = verify_album_publication(output)

        self.assertFalse(report.ok)
        self.assertIn("tags differ from immutable provenance", report.mismatches[0].message)

    def test_coherent_audio_and_manifest_rewrite_cannot_strip_required_artwork(
        self,
    ) -> None:
        plan_path, _source = self._fixture(("corrected-lossless",), artwork=True)
        output = self.root / "published"
        execute_album_publication_plan(plan_path, output)
        target = next((output / "corrected-lossless").glob("*.flac"))
        replacement = target.with_name(f"{target.stem}-rewrite.flac")
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(target),
                "-map",
                "0:a:0",
                "-map_metadata",
                "0",
                "-c:a",
                "copy",
                str(replacement),
            ],
            check=True,
        )
        target.unlink()
        replacement.rename(target)
        self._refresh_audio_inventory(output, target)

        report = verify_album_publication(output)

        self.assertFalse(report.ok)
        self.assertIn(
            "artwork differs from immutable provenance",
            report.mismatches[0].message,
        )

    def test_every_artifact_receipt_is_reasserted_after_deep_reads(self) -> None:
        plan_path, _source = self._fixture(("archival-source",))
        output = self.root / "published"
        execute_album_publication_plan(plan_path, output)
        real_validator = durability_module._validate_navigation_from_provenance

        def mutate_after_navigation(*args, **kwargs) -> None:
            real_validator(*args, **kwargs)
            cue = output / "album.cue"
            cue.write_text(
                cue.read_text(encoding="utf-8") + "REM MUTATED\n",
                encoding="utf-8",
            )

        with mock.patch.object(
            durability_module,
            "_validate_navigation_from_provenance",
            side_effect=mutate_after_navigation,
        ):
            report = verify_album_publication(output)

        self.assertFalse(report.ok)
        self.assertIn("changed during export", report.mismatches[0].message)

    def test_replay_executes_sibling_plan_and_matches_without_blessing(self) -> None:
        plan_path, _source = self._fixture(("archival-source", "corrected-lossless", "portable"))
        output = self.root / "published"
        execute_album_publication_plan(plan_path, output)
        replay = self.root / "replayed"

        report = replay_album_publication(output, replay, plan_path=plan_path)

        self.assertTrue(report.ok, report.mismatches)
        self.assertTrue(verify_album_publication(output).ok)
        self.assertTrue(verify_album_publication(replay).ok)

    def test_shared_source_replay_reopens_one_object_and_preserves_inputs(self) -> None:
        plan_path, source = self._fixture(
            ("archival-source",),
            second_side="shared",
        )
        output = self.root / "published-shared"
        replay = self.root / "replayed-shared"
        source_before = (
            source.read_bytes(),
            source.stat().st_mtime_ns,
            source.stat().st_ctime_ns,
        )

        execute_album_publication_plan(plan_path, output)
        original_before_verify = self._tree_receipts(output)
        first_verify = verify_album_publication(output)
        report = replay_album_publication(output, replay, plan_path=plan_path)
        reopened_verify = verify_album_publication(output)
        replay_verify = verify_album_publication(replay)

        self.assertTrue(first_verify.ok, first_verify.mismatches)
        self.assertTrue(report.ok, report.mismatches)
        self.assertTrue(reopened_verify.ok, reopened_verify.mismatches)
        self.assertTrue(replay_verify.ok, replay_verify.mismatches)
        self.assertEqual(original_before_verify, self._tree_receipts(output))
        original_manifest = json.loads(
            (output / "groove-serpent-album-publication.json").read_text(encoding="utf-8")
        )
        replay_manifest = json.loads(
            (replay / "groove-serpent-album-publication.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            original_manifest["archival_sources"],
            replay_manifest["archival_sources"],
        )
        self.assertEqual(len(original_manifest["archival_sources"]["objects"]), 1)
        self.assertEqual(
            source_before,
            (
                source.read_bytes(),
                source.stat().st_mtime_ns,
                source.stat().st_ctime_ns,
            ),
        )

    def test_tampered_archival_side_mapping_is_rejected(self) -> None:
        plan_path, _source = self._fixture(
            ("archival-source",),
            second_side="shared",
        )
        output = self.root / "published-shared"
        execute_album_publication_plan(plan_path, output)
        manifest_path = output / "groove-serpent-album-publication.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["archival_sources"]["side_bindings"][1]["source_object_id"] = (
            "source-99-000000000000"
        )
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        report = verify_album_publication(output)

        self.assertFalse(report.ok)
        self.assertIn(
            "binding differs from immutable provenance",
            report.mismatches[0].message,
        )

    def test_coherently_rewritten_archival_object_cannot_change_source_identity(
        self,
    ) -> None:
        plan_path, _source = self._fixture(
            ("archival-source",),
            second_side="shared",
        )
        output = self.root / "published-shared"
        execute_album_publication_plan(plan_path, output)
        target = next((output / "archival-source").iterdir())
        replacement = target.with_name("replacement.flac")
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=211:sample_rate=48000:duration=0.4",
                "-ac",
                "2",
                "-c:a",
                "flac",
                "-sample_fmt",
                "s16",
                str(replacement),
            ],
            check=True,
        )
        target.unlink()
        replacement.rename(target)
        self._refresh_audio_inventory(output, target)
        manifest_path = output / "groove-serpent-album-publication.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        item = next(
            value for value in manifest["inventory"] if value["role"] == "full-capture-source"
        )
        source_object = manifest["archival_sources"]["objects"][0]
        source_object["source_sha256"] = item["sha256"]
        source_object["source_size_bytes"] = item["size_bytes"]
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        report = verify_album_publication(output)

        self.assertFalse(report.ok)
        self.assertIn(
            "differs from inventory or immutable provenance",
            report.mismatches[0].message,
        )

    def test_exact_legacy_v1_manifest_remains_strictly_verifiable(self) -> None:
        plan_path, _source = self._fixture(("archival-source",))
        output = self.root / "published"
        execute_album_publication_plan(plan_path, output)
        manifest_path = output / "groove-serpent-album-publication.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema"] = "groove-serpent.album-publication-manifest/1"
        manifest.pop("archival_sources")
        item = next(
            value for value in manifest["inventory"] if value["role"] == "full-capture-source"
        )
        item["side_order"] = item.pop("first_side_order")
        item["side_label"] = "A"
        item.pop("source_object_id")
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        report = verify_album_publication(output)

        self.assertTrue(report.ok, report.mismatches)

    def _kill_at_boundary(self, plan_path: Path, output: Path, boundary: str) -> int:
        script = (
            "import os\n"
            "from pathlib import Path\n"
            "from groove_serpent.album_publication_executor import "
            "execute_album_publication_plan\n"
            f"boundary={boundary!r}\n"
            "def kill(value):\n"
            "    if value == boundary:\n"
            "        os._exit(77)\n"
            f"execute_album_publication_plan(Path({str(plan_path)!r}), "
            f"Path({str(output)!r}), fault_injector=kill)\n"
        )
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).parents[1],
            check=False,
        ).returncode

    def test_killed_staging_journal_and_precommit_states_are_inventory_safe(
        self,
    ) -> None:
        plan_path, _source = self._fixture(("archival-source",))
        cases = (
            ("after-stage-created", False, None),
            ("after-journal-staging", True, "staging"),
            ("before-commit", True, "verified-ready"),
        )
        for index, (boundary, owned, state) in enumerate(cases, start=1):
            output = self.root / f"killed-{index}"
            with self.subTest(boundary=boundary):
                self.assertEqual(
                    self._kill_at_boundary(plan_path, output, boundary),
                    77,
                )
                inventory = inventory_album_publication_orphans(self.root)
                candidates = [
                    item
                    for item in inventory.orphans
                    if item.intended_output_name == output.name
                    or (not owned and item.kind == "partial")
                ]
                self.assertTrue(candidates)
                orphan = candidates[-1]
                self.assertEqual(orphan.owned, owned)
                self.assertEqual(orphan.state, state)
                self.assertFalse(output.exists())
                if owned:
                    assert orphan.directory_identity is not None
                    assert orphan.journal_sha256 is not None
                    recovered = recover_album_publication_orphan(
                        Path(orphan.path),
                        expected_identity=orphan.directory_identity,
                        expected_journal_sha256=orphan.journal_sha256,
                        action="remove",
                    )
                    self.assertTrue(recovered.removed)

    def test_kill_after_commit_leaves_final_and_no_owned_partial(self) -> None:
        plan_path, _source = self._fixture(("archival-source",))
        output = self.root / "committed"

        self.assertEqual(self._kill_at_boundary(plan_path, output, "after-commit"), 77)

        self.assertTrue(verify_album_publication(output).ok)
        inventory = inventory_album_publication_orphans(self.root)
        self.assertFalse(
            any(item.intended_output_name == output.name for item in inventory.orphans)
        )

    def test_recovery_requires_exact_identity_and_journal_hash(self) -> None:
        plan_path, _source = self._fixture(("archival-source",))
        output = self.root / "killed"
        self.assertEqual(
            self._kill_at_boundary(plan_path, output, "after-journal-staging"),
            77,
        )
        orphan = next(
            item for item in inventory_album_publication_orphans(self.root).orphans if item.owned
        )
        assert orphan.directory_identity is not None

        with self.assertRaises(ExportError):
            recover_album_publication_orphan(
                Path(orphan.path),
                expected_identity=orphan.directory_identity,
                expected_journal_sha256="0" * 64,
                action="remove",
            )

        self.assertTrue(Path(orphan.path).is_dir())

    def test_orphan_inventory_rejects_parent_identity_substitution(self) -> None:
        real_identity = durability_module._directory_identity
        parent_calls = 0

        def substituted_identity(path: Path, *, label: str):
            nonlocal parent_calls
            identity = real_identity(path, label=label)
            if Path(path) == self.root:
                parent_calls += 1
                if parent_calls > 1:
                    return replace(identity, inode=identity.inode + 1)
            return identity

        with mock.patch.object(
            durability_module,
            "_directory_identity",
            side_effect=substituted_identity,
        ):
            with self.assertRaisesRegex(ExportError, "substituted during recovery"):
                inventory_album_publication_orphans(self.root)

    def test_recovery_rejects_parent_substitution_before_mutation(self) -> None:
        plan_path, _source = self._fixture(("archival-source",))
        output = self.root / "killed"
        self.assertEqual(
            self._kill_at_boundary(plan_path, output, "after-journal-staging"),
            77,
        )
        orphan = next(
            item for item in inventory_album_publication_orphans(self.root).orphans if item.owned
        )
        assert orphan.directory_identity is not None
        assert orphan.journal_sha256 is not None
        orphan_path = Path(orphan.path)
        real_identity = durability_module._directory_identity
        parent_calls = 0

        def substituted_identity(path: Path, *, label: str):
            nonlocal parent_calls
            identity = real_identity(path, label=label)
            if Path(path) == self.root:
                parent_calls += 1
                if parent_calls > 1:
                    return replace(identity, inode=identity.inode + 1)
            return identity

        with mock.patch.object(
            durability_module,
            "_directory_identity",
            side_effect=substituted_identity,
        ):
            with self.assertRaisesRegex(ExportError, "substituted during recovery"):
                recover_album_publication_orphan(
                    orphan_path,
                    expected_identity=orphan.directory_identity,
                    expected_journal_sha256=orphan.journal_sha256,
                    action="remove",
                )

        self.assertTrue(orphan_path.is_dir())


if __name__ == "__main__":
    unittest.main()
