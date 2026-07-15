from __future__ import annotations

import unittest
from dataclasses import replace

from groove_serpent.album import AlbumProject, AlbumSide
from groove_serpent.album_publication_navigation import (
    ALBUM_PUBLICATION_CHAPTERS_SCHEMA,
    NavigationSide,
    NavigationTrack,
    build_album_chapters,
    navigation_sides_from_publication,
    render_album_cue,
)
from groove_serpent.errors import ExportError
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)


_PLAN = "1" * 64
_ALBUM = "2" * 64


def _track(
    number: int,
    *,
    path: str,
    file_start: int,
    file_end: int,
    side_start: int,
    side_end: int,
    file_count: int,
) -> NavigationTrack:
    return NavigationTrack(
        album_track_number=number,
        local_track_number=number,
        title=f"Track {number}",
        artist='Artist "Name"',
        file_path=path,
        file_sha256=f"{number}" * 64,
        file_sample_count=file_count,
        source_start_sample=10_000 + side_start,
        source_end_sample=10_000 + side_end,
        side_output_start_sample=side_start,
        side_output_end_sample=side_end,
        file_output_start_sample=file_start,
        file_output_end_sample=file_end,
    )


class AlbumPublicationNavigationTests(unittest.TestCase):
    def _project(self) -> Project:
        return Project(
            source=AudioSource(
                path="side.flac",
                filename="side.flac",
                size_bytes=100,
                modified_ns=1,
                duration_seconds=3.0,
                sample_rate=48_000,
                channels=2,
                codec_name="flac",
                bits_per_raw_sample=16,
                sample_format="s16",
                sample_count=144_000,
                sha256="a" * 64,
            ),
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(
                music_start_seconds=0.5,
                music_end_seconds=2.5,
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=[
                Track(1, "First", 24_000, 72_000, 0.5, 1.5, artist="Artist"),
                Track(2, "Second", 72_000, 120_000, 1.5, 2.5, artist="Artist"),
            ],
        )

    def test_exact_chapters_and_multifile_cue(self) -> None:
        first = _track(
            1,
            path="corrected-lossless/01 - First.flac",
            file_start=0,
            file_end=48_000,
            side_start=0,
            side_end=48_000,
            file_count=48_000,
        )
        second = _track(
            2,
            path="corrected-lossless/02 - Second.flac",
            file_start=0,
            file_end=96_000,
            side_start=48_000,
            side_end=144_000,
            file_count=96_000,
        )
        side = NavigationSide(
            order=1,
            label="A",
            source_sample_rate=48_000,
            output_sample_rate=48_000,
            timeline_origin="corrected-music-range",
            tracks=(first, second),
        )

        chapters = build_album_chapters(
            plan_sha256=_PLAN,
            album_sha256=_ALBUM,
            basis_profile="corrected-lossless",
            metadata={"album": "Album", "artist": "Artist"},
            sides=(side,),
        )
        cue = render_album_cue(
            metadata={"album": "Album", "artist": "Artist"},
            sides=(side,),
        )

        self.assertEqual(chapters["schema"], ALBUM_PUBLICATION_CHAPTERS_SCHEMA)
        self.assertEqual(chapters["total_tracks"], 2)
        self.assertEqual(
            chapters["sides"][0]["tracks"][1]["side_output_start_sample"],
            48_000,
        )
        self.assertEqual(cue.count("FILE "), 2)
        self.assertEqual(cue.count("INDEX 01 00:00:00"), 2)
        self.assertIn("PERFORMER \"Artist ''Name''\"", cue)

    def test_continuous_side_cue_uses_exact_file_relative_indexes(self) -> None:
        first = _track(
            1,
            path="restored-side/01-A.flac",
            file_start=0,
            file_end=48_000,
            side_start=0,
            side_end=48_000,
            file_count=144_000,
        )
        second = replace(
            _track(
                2,
                path="restored-side/01-A.flac",
                file_start=48_000,
                file_end=144_000,
                side_start=48_000,
                side_end=144_000,
                file_count=144_000,
            ),
            file_sha256=first.file_sha256,
        )
        side = NavigationSide(
            1,
            "A",
            48_000,
            48_000,
            "project-music-range",
            (first, second),
        )

        cue = render_album_cue(metadata={"album": "Album"}, sides=(side,))

        self.assertEqual(cue.count("FILE "), 1)
        self.assertIn("INDEX 01 00:01:00", cue)

    def test_non_adjacent_side_timeline_is_rejected(self) -> None:
        first = _track(
            1,
            path="one.flac",
            file_start=0,
            file_end=10,
            side_start=0,
            side_end=10,
            file_count=10,
        )
        second = _track(
            2,
            path="two.flac",
            file_start=0,
            file_end=10,
            side_start=11,
            side_end=21,
            file_count=10,
        )
        side = NavigationSide(
            1,
            "A",
            44_100,
            44_100,
            "corrected-music-range",
            (first, second),
        )

        with self.assertRaisesRegex(ExportError, "adjacent"):
            build_album_chapters(
                plan_sha256=_PLAN,
                album_sha256=_ALBUM,
                basis_profile="corrected-lossless",
                metadata={},
                sides=(side,),
            )

    def test_path_escape_and_range_mismatch_are_rejected(self) -> None:
        escaping = _track(
            1,
            path="../outside.flac",
            file_start=0,
            file_end=10,
            side_start=0,
            side_end=10,
            file_count=10,
        )
        mismatch = replace(
            escaping,
            file_path="inside.flac",
            file_output_end_sample=9,
        )

        with self.assertRaisesRegex(ExportError, "remain inside"):
            escaping.validate()
        with self.assertRaisesRegex(ExportError, "different lengths"):
            mismatch.validate()

    def test_portable_equivalent_audio_paths_cannot_name_different_files(self) -> None:
        first = _track(
            1,
            path="corrected-lossless/Track.flac",
            file_start=0,
            file_end=10,
            side_start=0,
            side_end=10,
            file_count=10,
        )
        second = _track(
            2,
            path="corrected-lossless/track.flac",
            file_start=0,
            file_end=10,
            side_start=10,
            side_end=20,
            file_count=10,
        )
        side = NavigationSide(
            1,
            "A",
            44_100,
            44_100,
            "corrected-music-range",
            (first, second),
        )

        with self.assertRaisesRegex(ExportError, "collide"):
            build_album_chapters(
                plan_sha256=_PLAN,
                album_sha256=_ALBUM,
                basis_profile="corrected-lossless",
                metadata={},
                sides=(side,),
            )

    def test_corrected_inventory_is_bound_to_exact_navigation_geometry(self) -> None:
        album = AlbumProject(
            metadata={"artist": "Album Artist", "album": "Album"},
            sides=[AlbumSide("A", 1, "side.groove.json")],
        )
        inventory = [
            {
                "profile": "corrected-lossless",
                "role": "corrected-track",
                "path": "corrected-lossless/01 - First.flac",
                "sha256": "1" * 64,
                "side_label": "A",
                "local_track_number": 1,
                "album_track_number": 1,
                "source_start_sample": 24_000,
                "source_end_sample": 72_000,
                "corrected_start_sample": 0,
                "corrected_end_sample": 50_000,
                "verification": {"exact_sample_count": 50_000},
            },
            {
                "profile": "corrected-lossless",
                "role": "corrected-track",
                "path": "corrected-lossless/02 - Second.flac",
                "sha256": "2" * 64,
                "side_label": "A",
                "local_track_number": 2,
                "album_track_number": 2,
                "source_start_sample": 72_000,
                "source_end_sample": 120_000,
                "corrected_start_sample": 50_000,
                "corrected_end_sample": 100_000,
                "verification": {"exact_sample_count": 50_000},
            },
        ]

        basis, sides = navigation_sides_from_publication(
            album=album,
            projects_by_label={"A": self._project()},
            selected_profiles=("corrected-lossless",),
            inventory=inventory,
        )

        self.assertEqual(basis, "corrected-lossless")
        self.assertEqual(sides[0].tracks[1].source_start_sample, 72_000)
        self.assertEqual(sides[0].tracks[1].side_output_start_sample, 50_000)
        self.assertEqual(sides[0].tracks[1].file_output_start_sample, 0)

    def test_restored_navigation_excludes_leadin_and_runout(self) -> None:
        album = AlbumProject(
            metadata={"artist": "Album Artist", "album": "Album"},
            sides=[AlbumSide("A", 1, "side.groove.json")],
        )
        inventory = [
            {
                "profile": "restored-side",
                "role": "music-range-side",
                "path": "restored-side/01-A.flac",
                "sha256": "3" * 64,
                "side_label": "A",
                "verification": {"exact_sample_count": 96_000},
            }
        ]

        basis, sides = navigation_sides_from_publication(
            album=album,
            projects_by_label={"A": self._project()},
            selected_profiles=("restored-side",),
            inventory=inventory,
        )

        self.assertEqual(basis, "restored-side")
        self.assertEqual(sides[0].tracks[0].file_output_start_sample, 0)
        self.assertEqual(sides[0].tracks[-1].file_output_end_sample, 96_000)
        self.assertEqual(sides[0].tracks[-1].file_sample_count, 96_000)


if __name__ == "__main__":
    unittest.main()
