from __future__ import annotations

import copy
import hashlib
import json
import unittest

from groove_serpent.errors import ProjectValidationError
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    BoundaryCandidate,
    Project,
    Track,
)
from groove_serpent.topology import (
    TOPOLOGY_PROPOSAL_SCHEMA,
    propose_topology_refit,
    tracks_from_topology_proposal,
)


RECORDING_IDS = [
    "05df1765-62c0-4977-8959-bea4465e7e93",
    "4ddc4e4e-4c94-442c-8c2d-c10d44591a33",
    "64f8f9f7-199f-4df0-a962-46046ca1f4ef",
    "81eb2b6a-341a-4f1a-b75f-c2be4ab6207c",
]
TRACK_IDS = [
    "f02df099-2df0-37e3-b388-0eadc5175af3",
    "2349e6e4-3d4e-43ac-ac61-3f6d89ea1a98",
    "019ca19d-cb8d-4f79-97de-74d978bcc16d",
    "b7b0dbee-0c33-45e3-a240-af8da1f1de15",
]


def _candidate(
    cut_seconds: float,
    *,
    sample_rate: int = 100,
    score: float = 0.9,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> BoundaryCandidate:
    start = cut_seconds - 1.0 if start_seconds is None else start_seconds
    end = cut_seconds + 1.0 if end_seconds is None else end_seconds
    return BoundaryCandidate(
        start_seconds=start,
        end_seconds=end,
        cut_seconds=cut_seconds,
        cut_sample=round(cut_seconds * sample_rate),
        duration_seconds=end - start,
        minimum_db=-61.0,
        mean_db=-55.0,
        contrast_db=20.0,
        score=score,
    )


def _project(
    *,
    end_seconds: float = 300.0,
    current_boundaries: list[float] | None = None,
    candidates: list[BoundaryCandidate] | None = None,
    minimum_track_seconds: float = 10.0,
    current_sides: list[str] | None = None,
) -> Project:
    sample_rate = 100
    current_boundaries = current_boundaries or [100.0, 200.0]
    samples = [
        0,
        *[round(value * sample_rate) for value in current_boundaries],
        round(end_seconds * sample_rate),
    ]
    tracks: list[Track] = []
    for index, (start, end) in enumerate(zip(samples, samples[1:]), start=1):
        tracks.append(
            Track(
                number=index,
                title=f"Old {index}",
                start_sample=start,
                end_sample=end,
                start_seconds=start / sample_rate,
                end_seconds=end / sample_rate,
                confidence=0.8,
                artist="Old Artist",
                album="Old Album",
                side=(current_sides[index - 1] if current_sides else ""),
            )
        )
    source = AudioSource(
        path="record.flac",
        filename="record.flac",
        size_bytes=123456,
        modified_ns=99,
        duration_seconds=end_seconds,
        sample_rate=sample_rate,
        channels=2,
        codec_name="flac",
        bits_per_raw_sample=24,
        sample_format="s32",
        sample_count=round(end_seconds * sample_rate),
        sha256=hashlib.sha256(b"immutable-source").hexdigest(),
    )
    return Project(
        source=source,
        settings=AnalysisSettings(min_track_seconds=minimum_track_seconds),
        analysis=AnalysisSummary(
            music_start_seconds=0.0,
            music_end_seconds=end_seconds,
            noise_floor_db=-58.0,
            silence_threshold_db=-50.0,
            active_threshold_db=-38.0,
            envelope_window_seconds=0.05,
            candidates=candidates
            if candidates is not None
            else [_candidate(100.0), _candidate(200.0)],
            waveform=[],
        ),
        tracks=tracks,
        metadata={"artist": "Fallback Artist", "album": "Fallback Album"},
    )


def _release(count: int, *, durations: list[float] | None = None) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for index in range(count):
        item: dict[str, object] = {
            "position": index + 1,
            "number": str(index + 1),
            "title": f"Release {index + 1}",
            "artist": "Release Artist",
            "recording_id": RECORDING_IDS[index],
            "track_id": TRACK_IDS[index],
        }
        if durations is not None:
            item["duration_seconds"] = durations[index]
        result.append(item)
    return result


class TopologyTests(unittest.TestCase):
    def test_same_count_proposal_is_complete_contiguous_and_deterministic(self) -> None:
        project = _project()
        metadata = _release(3)
        first = propose_topology_refit(project, metadata)
        second = propose_topology_refit(project, copy.deepcopy(metadata))

        self.assertEqual(first, second)
        self.assertEqual(first["schema"], TOPOLOGY_PROPOSAL_SCHEMA)
        self.assertEqual(first["operation"], "refit")
        self.assertEqual(len(first["boundaries"]), 2)
        tracks = tracks_from_topology_proposal(project, first)
        self.assertEqual(len(tracks), 3)
        self.assertEqual(tracks[0].start_sample, project.tracks[0].start_sample)
        self.assertEqual(tracks[-1].end_sample, project.tracks[-1].end_sample)
        self.assertTrue(
            all(left.end_sample == right.start_sample for left, right in zip(tracks, tracks[1:]))
        )
        self.assertEqual(tracks[0].musicbrainz_recording_id, RECORDING_IDS[0])
        self.assertEqual(tracks[0].album, "Fallback Album")

    def test_split_and_merge_change_count_without_mutating_project(self) -> None:
        split_project = _project(current_boundaries=[150.0])
        before_split = copy.deepcopy(split_project.to_dict())
        split = propose_topology_refit(split_project, _release(3))
        self.assertEqual(split["operation"], "split")
        self.assertEqual(len(tracks_from_topology_proposal(split_project, split)), 3)
        self.assertEqual(split_project.to_dict(), before_split)

        merge_project = _project()
        before_merge = copy.deepcopy(merge_project.to_dict())
        merge = propose_topology_refit(merge_project, _release(2))
        self.assertEqual(merge["operation"], "merge")
        self.assertEqual(len(tracks_from_topology_proposal(merge_project, merge)), 2)
        self.assertEqual(merge_project.to_dict(), before_merge)

    def test_expected_durations_align_inside_a_measured_gap(self) -> None:
        project = _project(
            candidates=[
                _candidate(55.0, start_seconds=45.0, end_seconds=65.0),
                _candidate(200.0),
            ]
        )
        proposal = propose_topology_refit(
            project,
            _release(3, durations=[50.0, 150.0, 100.0]),
        )
        first = proposal["boundaries"][0]
        self.assertEqual(first["chosen_sample"], 5_000)
        self.assertIsNotNone(first["candidate_match"])
        self.assertTrue(first["candidate_match"]["aligned_within_gap"])
        self.assertAlmostEqual(first["duration_residual_seconds"], 0.0)

    def test_partial_expected_durations_are_used_and_marked_as_imputed(self) -> None:
        project = _project(
            candidates=[
                _candidate(55.0, start_seconds=45.0, end_seconds=65.0),
                _candidate(200.0),
            ]
        )
        release = _release(3)
        release[0]["duration_seconds"] = 50.0
        proposal = propose_topology_refit(project, release)

        self.assertEqual(proposal["boundaries"][0]["chosen_sample"], 5_000)
        self.assertEqual(
            proposal["boundaries"][0]["duration_evidence"], "partial-imputed"
        )
        self.assertTrue(any("imputed" in warning for warning in proposal["warnings"]))

    def test_side_change_uses_the_measured_side_gap_anchor(self) -> None:
        candidates = [
            _candidate(40.5, score=0.8),
            _candidate(55.0, score=0.95),
            _candidate(115.0, score=0.95, start_seconds=100.0, end_seconds=130.0),
            _candidate(145.0, score=0.95),
            _candidate(160.5, score=0.8),
        ]
        project = _project(
            end_seconds=230.0,
            current_boundaries=[40.5, 115.0, 160.5],
            candidates=candidates,
            minimum_track_seconds=20.0,
            current_sides=["A", "A", "B", "B"],
        )
        release = _release(4, durations=[40.0, 60.0, 30.0, 70.0])
        for index, side in enumerate(["A", "A", "B", "B"]):
            release[index]["side"] = side
            release[index]["side_position"] = 1 + index - (0 if side == "A" else 2)
        proposal = propose_topology_refit(project, release)

        anchor = proposal["boundaries"][1]
        self.assertTrue(anchor["side_change"])
        self.assertEqual(anchor["chosen_sample"], 11_500)
        self.assertIsNotNone(anchor["candidate_match"])
        self.assertEqual(anchor["candidate_match"]["measured_start_sample"], 10_000)
        self.assertEqual(anchor["candidate_match"]["measured_end_sample"], 13_000)

    def test_stale_and_tampered_proposals_are_refused(self) -> None:
        project = _project()
        proposal = propose_topology_refit(project, _release(3))

        tampered = copy.deepcopy(proposal)
        tampered["boundaries"][0]["chosen_sample"] += 1
        with self.assertRaisesRegex(ProjectValidationError, "edited or is corrupt"):
            tracks_from_topology_proposal(project, tampered)

        # Recomputing the public content hash is still insufficient: apply
        # regenerates the evidence-backed proposal and refuses an invented cut.
        tampered = copy.deepcopy(proposal)
        tampered["boundaries"][0]["chosen_sample"] += 1
        digest_payload = dict(tampered)
        digest_payload.pop("proposal_id")
        digest_payload.pop("proposal_sha256")
        rendered = json.dumps(
            digest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        tampered["proposal_sha256"] = digest
        tampered["proposal_id"] = f"topology-{digest[:24]}"
        with self.assertRaisesRegex(ProjectValidationError, "cannot be reproduced"):
            tracks_from_topology_proposal(project, tampered)

        project.metadata["album"] = "Edited after proposal"
        with self.assertRaisesRegex(ProjectValidationError, "stale"):
            tracks_from_topology_proposal(project, proposal)

    def test_impossible_minimum_spacing_is_refused(self) -> None:
        project = _project(
            end_seconds=100.0,
            current_boundaries=[50.0],
            candidates=[_candidate(50.0)],
            minimum_track_seconds=40.0,
        )
        with self.assertRaisesRegex(ProjectValidationError, "impossible"):
            propose_topology_refit(project, _release(3))

    def test_strict_provider_validation_rejects_coercion_and_bad_ids(self) -> None:
        project = _project()
        malformed = _release(3)
        malformed[0]["duration_seconds"] = True
        with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
            propose_topology_refit(project, malformed)

        malformed = _release(3)
        malformed[0]["duration_seconds"] = "120"
        with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
            propose_topology_refit(project, malformed)

        malformed = _release(3)
        malformed[0]["recording_id"] = "not-a-uuid"
        with self.assertRaisesRegex(ProjectValidationError, "valid MusicBrainz UUID"):
            propose_topology_refit(project, malformed)

    def test_extreme_integer_durations_and_spacing_fail_cleanly(self) -> None:
        project = _project()
        huge = 10**400
        malformed = _release(3)
        malformed[0]["duration_seconds"] = huge
        with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
            propose_topology_refit(project, malformed)

        with self.assertRaisesRegex(ProjectValidationError, "finite JSON number"):
            propose_topology_refit(
                project,
                _release(3),
                min_track_seconds=huge,
            )

        malformed = _release(3)
        malformed[0]["surprise"] = "field"
        with self.assertRaisesRegex(ProjectValidationError, "unsupported field"):
            propose_topology_refit(project, malformed)

    def test_noncontiguous_and_partial_side_grouping_is_refused(self) -> None:
        project = _project()
        noncontiguous = _release(3)
        for item, side in zip(noncontiguous, ["A", "B", "A"]):
            item["side"] = side
        with self.assertRaisesRegex(ProjectValidationError, "noncontiguous duplicate"):
            propose_topology_refit(project, noncontiguous)

        partial = _release(3)
        partial[0]["side"] = "A"
        with self.assertRaisesRegex(ProjectValidationError, "every release track"):
            propose_topology_refit(project, partial)


if __name__ == "__main__":
    unittest.main()
