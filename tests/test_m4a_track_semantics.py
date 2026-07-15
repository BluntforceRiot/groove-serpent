from __future__ import annotations

import unittest
from pathlib import Path

from groove_serpent.album_publication_durability import _expected_semantic_tags
from groove_serpent.errors import ExportError
from groove_serpent.exporter import _build_command, _verify_track_numbering_tags
from groove_serpent.models import Track


class M4ATrackSemanticTests(unittest.TestCase):
    def test_standard_m4a_track_atom_carries_number_and_total(self) -> None:
        _verify_track_numbering_tags(
            {"track": "2/9"},
            expected_track_number=2,
            expected_total_tracks=9,
            output_format="m4a",
        )

    def test_m4a_rejects_track_number_without_any_total(self) -> None:
        with self.assertRaisesRegex(ExportError, "does not include a total"):
            _verify_track_numbering_tags(
                {"track": "2"},
                expected_track_number=2,
                expected_total_tracks=9,
                output_format="m4a",
            )

    def test_conflicting_redundant_total_is_rejected(self) -> None:
        with self.assertRaisesRegex(ExportError, "conflicting"):
            _verify_track_numbering_tags(
                {"track": "2/9", "tracktotal": "8"},
                expected_track_number=2,
                expected_total_tracks=9,
                output_format="m4a",
            )

    def test_flac_requires_its_separate_tracktotal_comment(self) -> None:
        with self.assertRaisesRegex(ExportError, "TRACKTOTAL"):
            _verify_track_numbering_tags(
                {"track": "2/9"},
                expected_track_number=2,
                expected_total_tracks=9,
                output_format="flac",
            )

    def test_publication_expects_native_portable_track_semantics(self) -> None:
        track = Track(2, "Two", 0, 48_000, 0.0, 1.0)

        lossless = _expected_semantic_tags(track, 9, {}, portable=False)
        portable = _expected_semantic_tags(track, 9, {}, portable=True)

        self.assertEqual(lossless["track"], "2/9")
        self.assertEqual(lossless["tracktotal"], "9")
        self.assertEqual(portable["track"], "2/9")
        self.assertNotIn("tracktotal", portable)

    def test_m4a_command_keeps_standard_container_metadata_mode(self) -> None:
        track = Track(2, "Two", 0, 48_000, 0.0, 1.0)
        command = _build_command(
            source_path=Path("capture.flac"),
            output_path=Path("track.m4a"),
            track=track,
            total_tracks=9,
            output_format="m4a",
            source_sample_rate=48_000,
            source_bits=24,
            overwrite=False,
            flac_compression=8,
            aac_bitrate="256k",
        )

        self.assertEqual(command[command.index("-movflags") + 1], "+faststart")
        self.assertNotIn("use_metadata_tags", " ".join(command))


if __name__ == "__main__":
    unittest.main()
