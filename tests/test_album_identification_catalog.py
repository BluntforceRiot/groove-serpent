from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from groove_serpent.album import (
    AlbumProject,
    AlbumSide,
    load_album_project,
    repin_album_sides,
    save_album_project,
)
from groove_serpent.album_identification import (
    AlbumIdentificationConfig,
    ManualReleaseCandidate,
    capture_album_identification_context,
    propose_album_release_identification,
)
from groove_serpent.album_identification_catalog import (
    PROPOSAL_FILENAME_PREFIX,
    PROPOSAL_FILENAME_SUFFIX,
    album_identification_proposal_path,
    discover_album_identification_proposal_catalog,
    load_album_identification_proposal_file,
    load_current_album_identification_proposal,
    save_album_identification_proposal,
)
from groove_serpent.errors import ProjectValidationError
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.project_io import load_project, save_project
from groove_serpent.publication import canonical_json_sha256
from groove_serpent.recognition import RecognitionMatch


RELEASE_ID = "11111111-1111-4111-8111-111111111111"
GROUP_ID = "22222222-2222-4222-8222-222222222222"
RECORDING_IDS = (
    "33333333-3333-4333-8333-333333333333",
    "44444444-4444-4444-8444-444444444444",
)


def _canonical_bytes(value: dict[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _rehash(proposal: dict[str, object]) -> None:
    body = {key: value for key, value in proposal.items() if key != "proposal_sha256"}
    proposal["proposal_sha256"] = canonical_json_sha256(body)


class AlbumIdentificationCatalogTests(unittest.TestCase):
    def _write_project(self, root: Path, side: str) -> Path:
        source = root / f"side-{side}.flac"
        payload = (f"immutable-{side}".encode("utf-8")) * 16
        source.write_bytes(payload)
        details = source.stat()
        track = Track(
            number=1,
            title=f"Track {side}1",
            start_sample=0,
            end_sample=1_000,
            start_seconds=0.0,
            end_seconds=1.0,
            confidence=0.99,
            artist="Artist",
            album="Album",
            side=side,
        )
        project = Project(
            source=AudioSource(
                path=source.name,
                filename=source.name,
                size_bytes=details.st_size,
                modified_ns=details.st_mtime_ns,
                duration_seconds=1.0,
                sample_rate=1_000,
                channels=2,
                codec_name="flac",
                bits_per_raw_sample=24,
                sample_format="s32",
                sample_count=1_000,
                sha256=hashlib.sha256(payload).hexdigest(),
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
            tracks=[track],
            metadata={"artist": "Artist", "album": "Album", "side": side},
        )
        path = root / f"side-{side}.groove.json"
        save_project(project, path)
        return path

    def _case(
        self,
        root: Path,
        *,
        config: AlbumIdentificationConfig | None = None,
    ) -> tuple[Path, dict[str, object]]:
        side_a = self._write_project(root, "A")
        side_b = self._write_project(root, "B")
        album_path = root / "album.groove-album.json"
        album = AlbumProject(
            metadata={"artist": "Artist", "album": "Album"},
            sides=[
                AlbumSide("A", 1, side_a.name),
                AlbumSide("B", 2, side_b.name),
            ],
        )
        repin_album_sides(album, album_path)
        save_album_project(album, album_path)
        context = capture_album_identification_context(album_path)
        evidence = []
        for index, side in enumerate(context.sides):
            match = RecognitionMatch(
                provider="acoustid",
                title=f"Song {index + 1}",
                artist_credit="Artist",
                score=0.98,
                recording_mbid=RECORDING_IDS[index],
                release_group_ids=(GROUP_ID,),
                release_candidates=(
                    {
                        "release_mbid": RELEASE_ID,
                        "title": "Album",
                        "release_group_mbid": GROUP_ID,
                        "country": "US",
                        "date": "2006-06-13",
                        "status": "Official",
                    },
                ),
            )
            evidence.append(context.bind_track(side.label, 1, [match]))
        proposal = propose_album_release_identification(
            album_path,
            evidence,
            manual_candidates=[
                ManualReleaseCandidate(
                    title="Album",
                    source_description="Physical-copy owner notes",
                    release_mbid=RELEASE_ID,
                    country="US",
                    label="Record Label",
                    catalog_number="CAT-001",
                    matrix_runout="CAT-001-A",
                )
            ],
            config=config,
        )
        return album_path, proposal

    @staticmethod
    def _input_bytes(root: Path) -> dict[str, bytes]:
        return {
            path.name: path.read_bytes()
            for path in root.iterdir()
            if path.suffix in {".flac", ".json"}
            and not path.name.startswith(PROPOSAL_FILENAME_PREFIX)
        }

    @staticmethod
    def _write_external(
        album_path: Path,
        proposal: dict[str, object],
        *,
        filename: str | None = None,
        raw: bytes | None = None,
    ) -> Path:
        path = (
            album_identification_proposal_path(album_path, proposal)
            if filename is None
            else album_path.parent / filename
        )
        path.write_bytes(raw if raw is not None else _canonical_bytes(proposal))
        return path

    def test_save_reopen_discover_and_select_are_restart_safe_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path, proposal = self._case(root)
            before = self._input_bytes(root)

            proposal_path = save_album_identification_proposal(album_path, proposal)
            self.assertEqual(proposal_path.parent, album_path.parent.resolve())
            self.assertEqual(
                proposal_path.name,
                f"{PROPOSAL_FILENAME_PREFIX}{proposal['proposal_sha256']}"
                f"{PROPOSAL_FILENAME_SUFFIX}",
            )
            loaded = load_album_identification_proposal_file(proposal_path)
            self.assertEqual(loaded.proposal, proposal)
            self.assertEqual(loaded.raw, _canonical_bytes(proposal))

            catalog = discover_album_identification_proposal_catalog(album_path)
            self.assertTrue(catalog.scan_complete)
            self.assertTrue(catalog.live_context_available)
            self.assertEqual(len(catalog.entries), 1)
            entry = catalog.entries[0]
            self.assertEqual(entry.status, "current")
            self.assertTrue(entry.selectable)
            self.assertEqual(entry.manual_candidate_count, 1)
            self.assertFalse(loaded.proposal["authority"]["may_apply_metadata"])
            self.assertFalse(
                loaded.proposal["exact_pressing_review"]
                ["manual_candidates_affect_automatic_ranking"]
            )
            selected = load_current_album_identification_proposal(
                album_path,
                proposal_path,
                expected_file_sha256=entry.file_sha256,
            )
            self.assertEqual(selected.proposal, proposal)
            self.assertEqual(before, self._input_bytes(root))

    def test_existing_destination_and_same_name_race_never_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path, proposal = self._case(root)
            path = save_album_identification_proposal(album_path, proposal)
            original = path.read_bytes()
            with self.assertRaisesRegex(ProjectValidationError, "already exists"):
                save_album_identification_proposal(album_path, proposal)
            self.assertEqual(path.read_bytes(), original)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path, proposal = self._case(root)
            destination = album_identification_proposal_path(album_path, proposal)
            racer = b"racing process owns this file"

            def race(_source: Path, target: Path) -> None:
                target.write_bytes(racer)
                raise FileExistsError(target)

            with mock.patch(
                "groove_serpent.album_identification_catalog.rename_no_replace",
                side_effect=race,
            ), self.assertRaisesRegex(ProjectValidationError, "already exists"):
                save_album_identification_proposal(album_path, proposal)
            self.assertEqual(destination.read_bytes(), racer)

    def test_source_project_and_album_drift_are_stale_and_not_selectable(self) -> None:
        def source_drift(root: Path, _album_path: Path) -> None:
            source = root / "side-A.flac"
            source.write_bytes(b"X" * source.stat().st_size)

        def project_drift(root: Path, _album_path: Path) -> None:
            project_path = root / "side-A.groove.json"
            project = load_project(project_path)
            project.metadata["note"] = "changed"
            save_project(project, project_path)

        def album_drift(_root: Path, album_path: Path) -> None:
            album = load_album_project(album_path)
            album.metadata["note"] = "changed"
            save_album_project(album, album_path, overwrite=True)

        for label, mutate in (
            ("source", source_drift),
            ("project", project_drift),
            ("album", album_drift),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                album_path, proposal = self._case(root)
                path = save_album_identification_proposal(album_path, proposal)
                mutate(root, album_path)
                catalog = discover_album_identification_proposal_catalog(album_path)
                self.assertEqual(len(catalog.entries), 1)
                self.assertEqual(catalog.entries[0].status, "stale")
                self.assertFalse(catalog.entries[0].selectable)
                with self.assertRaises(ProjectValidationError):
                    load_current_album_identification_proposal(album_path, path)

    def test_algorithm_module_and_config_drift_are_stale(self) -> None:
        for field, replacement, issue_code in (
            ("id", "legacy-release-consensus/0", "algorithm_changed"),
            ("module_sha256", "f" * 64, "algorithm_module_bytes_changed"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                album_path, proposal = self._case(root)
                drifted = copy.deepcopy(proposal)
                drifted["algorithm"][field] = replacement
                _rehash(drifted)
                self._write_external(album_path, drifted)
                catalog = discover_album_identification_proposal_catalog(album_path)
                self.assertEqual(catalog.entries[0].status, "stale")
                self.assertIn(issue_code, [i.code for i in catalog.entries[0].issues])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            custom = AlbumIdentificationConfig(minimum_mean_score=0.80)
            album_path, proposal = self._case(root, config=custom)
            save_album_identification_proposal(album_path, proposal, config=custom)
            default_catalog = discover_album_identification_proposal_catalog(album_path)
            self.assertEqual(default_catalog.entries[0].status, "stale")
            self.assertIn(
                "config_changed",
                [issue.code for issue in default_catalog.entries[0].issues],
            )
            custom_catalog = discover_album_identification_proposal_catalog(
                album_path,
                config=custom,
            )
            self.assertEqual(custom_catalog.entries[0].status, "current")

    def test_semantic_tamper_with_recomputed_hash_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path, proposal = self._case(root)
            tampered = copy.deepcopy(proposal)
            tampered["ranked_release_candidates"][0]["evidence_score"] = 0.1
            _rehash(tampered)
            self._write_external(album_path, tampered)
            catalog = discover_album_identification_proposal_catalog(album_path)
            self.assertEqual(catalog.entries[0].status, "invalid")
            self.assertFalse(catalog.entries[0].selectable)
            self.assertIn(
                "invalid_proposal_semantics",
                [issue.code for issue in catalog.entries[0].issues],
            )

    def test_strict_json_size_depth_and_incomplete_scan_fail_closed(self) -> None:
        malformed_cases = {
            "duplicate": b'{"schema":"one","schema":"two"}\n',
            "nan": b'{"value":NaN}\n',
            "depth": json.dumps(
                {"value": [[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]},
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n",
        }
        for label, raw in malformed_cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                album_path, _proposal = self._case(root)
                filename = (
                    f"{PROPOSAL_FILENAME_PREFIX}{'a' * 64}{PROPOSAL_FILENAME_SUFFIX}"
                )
                (root / filename).write_bytes(raw)
                catalog = discover_album_identification_proposal_catalog(album_path)
                self.assertEqual(catalog.entries[0].status, "invalid")
                self.assertFalse(catalog.entries[0].selectable)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path, _proposal = self._case(root)
            filename = f"{PROPOSAL_FILENAME_PREFIX}{'b' * 64}{PROPOSAL_FILENAME_SUFFIX}"
            (root / filename).write_bytes(b"{}" * 100)
            with mock.patch(
                "groove_serpent.album_identification_catalog.MAX_PROPOSAL_BYTES",
                32,
            ):
                catalog = discover_album_identification_proposal_catalog(album_path)
            self.assertEqual(catalog.entries[0].status, "invalid")
            self.assertIn(
                "proposal_size_limit",
                [issue.code for issue in catalog.entries[0].issues],
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path, proposal = self._case(root)
            save_album_identification_proposal(album_path, proposal)
            (root / "unrelated.txt").write_text("x", encoding="utf-8")
            with mock.patch(
                "groove_serpent.album_identification_catalog.MAX_DIRECTORY_ENTRIES",
                1,
            ):
                catalog = discover_album_identification_proposal_catalog(album_path)
            self.assertFalse(catalog.scan_complete)
            self.assertEqual(catalog.entries, ())

    def test_symlink_hardlink_reparse_and_portable_collisions_are_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path, proposal = self._case(root)
            original = save_album_identification_proposal(album_path, proposal)
            alias = root / (
                f"{PROPOSAL_FILENAME_PREFIX}{'a' * 64}{PROPOSAL_FILENAME_SUFFIX}"
            )
            try:
                alias.symlink_to(original.name)
            except OSError:
                pass
            else:
                catalog = discover_album_identification_proposal_catalog(album_path)
                invalid = [
                    entry for entry in catalog.entries if entry.filename == alias.name
                ]
                self.assertEqual(invalid[0].status, "invalid")
                self.assertIn(
                    "unsafe_reparse_entry",
                    [i.code for i in invalid[0].issues],
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path, proposal = self._case(root)
            original = save_album_identification_proposal(album_path, proposal)
            hardlink = root / (
                f"{PROPOSAL_FILENAME_PREFIX}{'b' * 64}{PROPOSAL_FILENAME_SUFFIX}"
            )
            os.link(original, hardlink)
            catalog = discover_album_identification_proposal_catalog(album_path)
            self.assertTrue(all(entry.status == "invalid" for entry in catalog.entries))
            self.assertTrue(all(not entry.selectable for entry in catalog.entries))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path, proposal = self._case(root)
            save_album_identification_proposal(album_path, proposal)
            with mock.patch(
                "groove_serpent.album_identification_catalog._is_reparse",
                return_value=True,
            ):
                catalog = discover_album_identification_proposal_catalog(album_path)
            self.assertEqual(catalog.entries[0].status, "invalid")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path, proposal = self._case(root)
            save_album_identification_proposal(album_path, proposal)
            drifted = copy.deepcopy(proposal)
            drifted["algorithm"]["module_sha256"] = "e" * 64
            _rehash(drifted)
            self._write_external(album_path, drifted)
            with mock.patch(
                "groove_serpent.album_identification_catalog.portable_name_key",
                return_value="forced-portable-collision",
            ):
                catalog = discover_album_identification_proposal_catalog(album_path)
            self.assertTrue(all(entry.status == "invalid" for entry in catalog.entries))
            self.assertTrue(
                all(
                    "portable_name_collision" in [issue.code for issue in entry.issues]
                    for entry in catalog.entries
                )
            )

    def test_filename_identity_noncanonical_serialization_and_expected_hash_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path, proposal = self._case(root)
            wrong_name = (
                f"{PROPOSAL_FILENAME_PREFIX}{'f' * 64}{PROPOSAL_FILENAME_SUFFIX}"
            )
            self._write_external(album_path, proposal, filename=wrong_name)
            catalog = discover_album_identification_proposal_catalog(album_path)
            self.assertEqual(catalog.entries[0].status, "invalid")
            self.assertIn(
                "filename_identity_mismatch",
                [issue.code for issue in catalog.entries[0].issues],
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path, proposal = self._case(root)
            pretty = (json.dumps(proposal, indent=2) + "\n").encode("utf-8")
            self._write_external(album_path, proposal, raw=pretty)
            catalog = discover_album_identification_proposal_catalog(album_path)
            self.assertEqual(catalog.entries[0].status, "invalid")
            self.assertIn(
                "noncanonical_serialization",
                [issue.code for issue in catalog.entries[0].issues],
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path, proposal = self._case(root)
            path = save_album_identification_proposal(album_path, proposal)
            with self.assertRaisesRegex(ProjectValidationError, "file changed"):
                load_current_album_identification_proposal(
                    album_path,
                    path,
                    expected_file_sha256="0" * 64,
                )

    def test_selection_recaptures_live_context_after_final_file_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path, proposal = self._case(root)
            path = save_album_identification_proposal(album_path, proposal)
            current = capture_album_identification_context(album_path)
            changed = replace(current, album_sha256="0" * 64)
            with mock.patch(
                "groove_serpent.album_identification_catalog."
                "capture_album_identification_context",
                side_effect=[current, changed],
            ), self.assertRaisesRegex(ProjectValidationError, "during proposal selection"):
                load_current_album_identification_proposal(album_path, path)


if __name__ == "__main__":
    unittest.main()
