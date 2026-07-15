from __future__ import annotations

import hashlib
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest import mock

from groove_serpent.album import (
    AlbumProject,
    AlbumSide,
    repin_album_sides,
    save_album_project,
)
from groove_serpent.album_identification import (
    ALBUM_IDENTIFICATION_EVIDENCE_SCHEMA,
    AlbumIdentificationConfig,
    ManualReleaseCandidate,
    RecognitionObservation,
    ReleaseCandidateFacts,
    TrackRecognitionEvidence,
    capture_album_identification_context,
    propose_album_release_identification,
    validate_album_identification_proposal,
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


RELEASE_1 = "11111111-1111-4111-8111-111111111111"
RELEASE_2 = "22222222-2222-4222-8222-222222222222"
GROUP_1 = "33333333-3333-4333-8333-333333333333"
GROUP_2 = "44444444-4444-4444-8444-444444444444"
RECORDINGS = (
    "55555555-5555-4555-8555-555555555555",
    "66666666-6666-4666-8666-666666666666",
    "77777777-7777-4777-8777-777777777777",
    "88888888-8888-4888-8888-888888888888",
)


class AlbumIdentificationTests(unittest.TestCase):
    def _write_project(self, root: Path, side: str, track_count: int = 2) -> Path:
        source = root / f"side-{side}.flac"
        payload = (f"immutable-side-{side}".encode("utf-8")) * 8
        source.write_bytes(payload)
        metadata = source.stat()
        tracks = [
            Track(
                number=index,
                title=f"Track {side}{index}",
                start_sample=(index - 1) * 1_000,
                end_sample=index * 1_000,
                start_seconds=float(index - 1),
                end_seconds=float(index),
                confidence=0.95,
                artist="Artist",
                album="Album",
                side=side,
            )
            for index in range(1, track_count + 1)
        ]
        project = Project(
            source=AudioSource(
                path=source.name,
                filename=source.name,
                size_bytes=metadata.st_size,
                modified_ns=metadata.st_mtime_ns,
                duration_seconds=float(track_count),
                sample_rate=1_000,
                channels=2,
                codec_name="flac",
                bits_per_raw_sample=24,
                sample_format="s32",
                sample_count=track_count * 1_000,
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(
                music_start_seconds=0.0,
                music_end_seconds=float(track_count),
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=tracks,
            metadata={"artist": "Artist", "album": "Album", "side": side},
        )
        project_path = root / f"side-{side}.groove.json"
        save_project(project, project_path)
        return project_path

    def _write_album(self, root: Path, track_count: int = 2) -> Path:
        side_a = self._write_project(root, "A", track_count)
        side_b = self._write_project(root, "B", track_count)
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
        return album_path

    @staticmethod
    def _match(
        track_index: int,
        release_id: str = RELEASE_1,
        *,
        score: float = 0.97,
        title: str = "Album",
        group_id: str = GROUP_1,
    ) -> RecognitionMatch:
        return RecognitionMatch(
            provider="acoustid",
            title=f"Song {track_index}",
            artist_credit="Artist",
            score=score,
            recording_mbid=RECORDINGS[track_index - 1],
            release_group_ids=(group_id,),
            release_candidates=(
                {
                    "release_mbid": release_id,
                    "title": title,
                    "release_group_mbid": group_id,
                    "country": "US",
                    "date": "2006-06-13",
                    "status": "Official",
                    "release_group_title": "Album",
                    "release_group_type": "Album",
                    "release_group_secondary_types": [],
                },
            ),
        )

    def _all_evidence(self, album_path: Path) -> list[TrackRecognitionEvidence]:
        context = capture_album_identification_context(album_path)
        evidence: list[TrackRecognitionEvidence] = []
        index = 1
        for side in context.sides:
            for track in side.tracks:
                evidence.append(
                    context.bind_track(side.label, track.number, [self._match(index)])
                )
                index += 1
        return evidence

    def test_cross_side_consensus_is_deterministic_but_has_no_write_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path = self._write_album(root)
            evidence = self._all_evidence(album_path)
            before = {path.name: path.read_bytes() for path in root.iterdir()}

            first = propose_album_release_identification(album_path, evidence)
            second = propose_album_release_identification(
                album_path,
                list(reversed(evidence)),
            )

            self.assertEqual(first, second)
            self.assertEqual(first["decision"]["status"], "proposed")
            self.assertEqual(first["decision"]["confidence"], "high")
            self.assertEqual(first["decision"]["selected_release_mbid"], RELEASE_1)
            ranked = first["ranked_release_candidates"][0]
            self.assertEqual(ranked["supporting_track_count"], 4)
            self.assertEqual(ranked["supporting_side_count"], 2)
            self.assertEqual(ranked["album_track_coverage"], 1.0)
            self.assertFalse(first["authority"]["may_apply_metadata"])
            self.assertFalse(first["authority"]["may_download_or_apply_artwork"])
            self.assertFalse(first["authority"]["physical_pressing_proven"])
            self.assertEqual(
                before,
                {path.name: path.read_bytes() for path in root.iterdir()},
            )

    def test_equal_cross_side_candidates_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            album_path = self._write_album(Path(directory))
            context = capture_album_identification_context(album_path)
            evidence: list[TrackRecognitionEvidence] = []
            index = 1
            for side in context.sides:
                for track in side.tracks:
                    evidence.append(
                        context.bind_track(
                            side.label,
                            track.number,
                            [
                                self._match(index, RELEASE_1, group_id=GROUP_1),
                                self._match(index, RELEASE_2, group_id=GROUP_2),
                            ],
                        )
                    )
                    index += 1
            proposal = propose_album_release_identification(album_path, evidence)
            self.assertEqual(proposal["decision"]["status"], "ambiguous")
            self.assertIsNone(proposal["decision"]["selected_release_mbid"])
            self.assertEqual(proposal["decision"]["rank_margin"], 0.0)
            self.assertEqual(
                [item["release_mbid"] for item in proposal["ranked_release_candidates"]],
                [RELEASE_1, RELEASE_2],
            )

    def test_single_side_or_low_score_evidence_abstains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            album_path = self._write_album(Path(directory))
            context = capture_album_identification_context(album_path)
            side = context.sides[0]
            evidence = [
                context.bind_track(
                    side.label,
                    track.number,
                    [self._match(track.number, score=0.60)],
                )
                for track in side.tracks
            ]
            proposal = propose_album_release_identification(album_path, evidence)
            self.assertEqual(proposal["decision"]["status"], "abstained")
            self.assertIn(
                "candidate_not_supported_across_independent_sides",
                proposal["decision"]["reasons"],
            )
            self.assertIn(
                "recognition_scores_too_low",
                proposal["decision"]["reasons"],
            )

    def test_conflicting_network_facts_are_excluded_not_silently_merged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            album_path = self._write_album(Path(directory), track_count=1)
            context = capture_album_identification_context(album_path)
            evidence = [
                context.bind_track("A", 1, [self._match(1, title="Album")]),
                context.bind_track("B", 1, [self._match(2, title="Different Album")]),
            ]
            proposal = propose_album_release_identification(album_path, evidence)
            self.assertEqual(proposal["decision"]["status"], "abstained")
            self.assertEqual(
                proposal["decision"]["reasons"],
                ["conflicting_network_release_facts_require_review"],
            )
            self.assertEqual(proposal["ranked_release_candidates"], [])
            self.assertEqual(
                proposal["excluded_conflicts"],
                [
                    {
                        "release_mbid": RELEASE_1,
                        "conflicting_fields": ["title"],
                        "disposition": "excluded-from-ranking",
                    }
                ],
            )

    def test_stale_project_source_and_track_bindings_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path = self._write_album(root)
            context = capture_album_identification_context(album_path)
            evidence = [context.bind_track("A", 1, [self._match(1)])]
            stale_range = replace(evidence[0], end_sample=evidence[0].end_sample + 1)
            with self.assertRaisesRegex(ProjectValidationError, "range or track state"):
                propose_album_release_identification(album_path, [stale_range])

            project_path = root / "side-A.groove.json"
            project = load_project(project_path)
            project.metadata["note"] = "changed"
            save_project(project, project_path)
            with self.assertRaisesRegex(ProjectValidationError, "stale"):
                propose_album_release_identification(album_path, evidence)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album_path = self._write_album(root)
            evidence = self._all_evidence(album_path)
            source = root / "side-A.flac"
            source.write_bytes(b"X" * source.stat().st_size)
            with self.assertRaisesRegex(ProjectValidationError, "source"):
                propose_album_release_identification(album_path, evidence)

    def test_duplicate_track_evidence_and_unbounded_matches_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            album_path = self._write_album(Path(directory))
            context = capture_album_identification_context(album_path)
            evidence = context.bind_track("A", 1, [self._match(1)])
            with self.assertRaisesRegex(ProjectValidationError, "only one"):
                propose_album_release_identification(album_path, [evidence, evidence])

            oversized = replace(
                evidence,
                observations=evidence.observations * 21,
            )
            with self.assertRaisesRegex(ProjectValidationError, "1-20"):
                oversized.validate()

    def test_strict_network_normalization_rejects_unknown_nan_and_non_uuid(self) -> None:
        valid = {
            "release_mbid": RELEASE_1,
            "title": "Album",
            "release_group_mbid": GROUP_1,
        }
        with self.assertRaisesRegex(ProjectValidationError, "unsupported"):
            ReleaseCandidateFacts.from_mapping({**valid, "remote_surprise": True})
        with self.assertRaisesRegex(ProjectValidationError, "exact MusicBrainz"):
            ReleaseCandidateFacts.from_mapping({"title": "Album"})
        with self.assertRaisesRegex(ProjectValidationError, "canonical"):
            ReleaseCandidateFacts.from_mapping({**valid, "release_mbid": "release-1"})
        with self.assertRaisesRegex(ProjectValidationError, "calendar date"):
            ReleaseCandidateFacts.from_mapping({**valid, "date": "2026-99-99"})

        match = self._match(1)
        bad_score = replace(
            RecognitionObservation.from_recognition_match(match),
            score=float("nan"),
        )
        with self.assertRaises(ProjectValidationError):
            bad_score.validate()

    def test_manual_candidate_is_preserved_but_never_affects_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            album_path = self._write_album(Path(directory), track_count=1)
            context = capture_album_identification_context(album_path)
            no_release = RecognitionMatch(
                provider="acoustid",
                title="Recognized recording",
                artist_credit="Artist",
                score=0.99,
                recording_mbid=RECORDINGS[0],
            )
            evidence = [context.bind_track("A", 1, [no_release])]
            manual = ManualReleaseCandidate(
                title="Album",
                source_description="Owner transcription from physical copy",
                release_mbid=RELEASE_1,
                artist_credit="Artist",
                country="US",
                date="2006",
                label="Record Label",
                catalog_number="CAT-001",
                media_formats=("12-inch Vinyl",),
                matrix_runout="ABC-001-A",
            )
            proposal = propose_album_release_identification(
                album_path,
                evidence,
                manual_candidates=[manual],
            )
            self.assertEqual(proposal["decision"]["status"], "abstained")
            self.assertEqual(proposal["ranked_release_candidates"], [])
            review = proposal["exact_pressing_review"]
            self.assertEqual(review["manual_candidates"][0]["matrix_runout"], "ABC-001-A")
            self.assertFalse(review["manual_candidates_affect_automatic_ranking"])
            self.assertEqual(
                review["manual_candidates"][0]["ranking_authority"],
                "none-review-input-only",
            )

    def test_evidence_round_trip_is_strict_and_proposal_semantics_are_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            album_path = self._write_album(Path(directory))
            evidence = self._all_evidence(album_path)[0]
            restored = TrackRecognitionEvidence.from_dict(evidence.to_dict())
            self.assertEqual(restored, evidence)
            self.assertEqual(restored.schema, ALBUM_IDENTIFICATION_EVIDENCE_SCHEMA)

            payload = evidence.to_dict()
            payload["extra"] = True
            with self.assertRaisesRegex(ProjectValidationError, "extra"):
                TrackRecognitionEvidence.from_dict(payload)

            proposal = propose_album_release_identification(
                album_path,
                self._all_evidence(album_path),
            )
            validate_album_identification_proposal(proposal)
            proposal["authority"]["may_apply_metadata"] = True
            body = {key: value for key, value in proposal.items() if key != "proposal_sha256"}
            proposal["proposal_sha256"] = canonical_json_sha256(body)
            with self.assertRaisesRegex(ProjectValidationError, "unsafe authority"):
                validate_album_identification_proposal(proposal)

            untampered = propose_album_release_identification(
                album_path,
                self._all_evidence(album_path),
            )
            ranking_tamper = deepcopy(untampered)
            ranking_tamper["ranked_release_candidates"][0]["evidence_score"] = 0.1
            tampered_body = {
                key: value
                for key, value in ranking_tamper.items()
                if key != "proposal_sha256"
            }
            ranking_tamper["proposal_sha256"] = canonical_json_sha256(tampered_body)
            with self.assertRaisesRegex(ProjectValidationError, "ranking is inconsistent"):
                validate_album_identification_proposal(ranking_tamper)

            speed_tamper = deepcopy(untampered)
            speed_tamper["album"]["sides"][0]["requested_speed_factor"] = 1.1
            speed_body = {
                key: value
                for key, value in speed_tamper.items()
                if key != "proposal_sha256"
            }
            speed_tamper["proposal_sha256"] = canonical_json_sha256(speed_body)
            with self.assertRaisesRegex(
                ProjectValidationError,
                "fingerprint asetrate is inconsistent",
            ):
                validate_album_identification_proposal(speed_tamper)

    def test_context_capture_and_proposal_do_not_invoke_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            album_path = self._write_album(Path(directory))
            evidence = self._all_evidence(album_path)
            with mock.patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network must not be called"),
            ) as opener:
                proposal = propose_album_release_identification(album_path, evidence)
            opener.assert_not_called()
            self.assertEqual(proposal["decision"]["status"], "proposed")

    def test_config_rejects_coercion_and_inverted_thresholds(self) -> None:
        with self.assertRaises(ProjectValidationError):
            AlbumIdentificationConfig(minimum_supporting_tracks=True).validate()
        with self.assertRaises(ProjectValidationError):
            AlbumIdentificationConfig(
                minimum_track_coverage=0.8,
                high_confidence_track_coverage=0.7,
            ).validate()

    def test_global_match_limit_is_enforced_before_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            album_path = self._write_album(Path(directory))
            evidence = self._all_evidence(album_path)
            with mock.patch(
                "groove_serpent.album_identification.MAX_TOTAL_MATCHES",
                3,
            ), self.assertRaisesRegex(ProjectValidationError, "total matches"):
                propose_album_release_identification(album_path, evidence)

    def test_evidence_sha_changes_with_exact_recording_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            album_path = self._write_album(Path(directory))
            context = capture_album_identification_context(album_path)
            first = context.bind_track("A", 1, [self._match(1, score=0.90)])
            second = context.bind_track("A", 1, [self._match(1, score=0.91)])
            self.assertNotEqual(first.sha256, second.sha256)
            self.assertEqual(first.sha256, canonical_json_sha256(first.to_dict()))


if __name__ == "__main__":
    unittest.main()
