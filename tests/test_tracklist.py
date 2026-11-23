from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from groove_serpent.errors import GrooveSerpentError
from groove_serpent.models import MAX_TRACKS
from groove_serpent.tracklist import (
    MAX_TRACKLIST_BYTES,
    MAX_TRACKLIST_LINE_BYTES,
    load_tracklist,
    parse_duration,
)


class TracklistTests(unittest.TestCase):
    def test_parse_duration(self) -> None:
        self.assertEqual(parse_duration("4:12"), 252.0)
        self.assertEqual(parse_duration("1:02:03"), 3723.0)
        for value in ("inf", "1e309", "4:99", "1:60:00", float("nan")):
            with self.subTest(value=value), self.assertRaises(GrooveSerpentError):
                parse_duration(value)
        with self.assertRaisesRegex(GrooveSerpentError, "finite"):
            parse_duration(10**10_000)
        self.assertEqual(parse_duration(90), 90.0)

    def test_json_tracklist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tracks.json"
            path.write_text(
                json.dumps(
                    {
                        "artist": "Example Artist",
                        "album": "Example Album",
                        "side": "A",
                        "tracks": [
                            {"title": "First", "duration": "4:12", "side": "A"},
                            {"title": "Second", "duration": 198, "side": "B"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = load_tracklist(path)
            self.assertEqual(result.metadata["album"], "Example Album")
            self.assertEqual([track.title for track in result.tracks], ["First", "Second"])
            self.assertEqual(result.tracks[0].duration_seconds, 252.0)
            self.assertEqual([track.side for track in result.tracks], ["A", "B"])

            path.write_text('{"tracks":"AB"}', encoding="utf-8")
            with self.assertRaisesRegex(GrooveSerpentError, "must be an array"):
                load_tracklist(path)

    def test_text_tracklist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tracks.txt"
            path.write_text(
                "Artist: Example Artist\nAlbum: Example Album\nSide: B\n"
                "01 | Opening Track | 3:45\n02 | Finale | 5:01\n",
                encoding="utf-8",
            )
            result = load_tracklist(path)
            self.assertEqual(result.metadata["side"], "B")
            self.assertEqual(result.tracks[1].title, "Finale")
            self.assertEqual(result.tracks[1].duration_seconds, 301.0)

    def test_oversized_file_is_rejected_before_decoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tracks.json"
            with path.open("wb") as source:
                source.truncate(MAX_TRACKLIST_BYTES + 1)
            with patch("groove_serpent.tracklist.json.JSONDecoder") as decoder:
                with self.assertRaisesRegex(GrooveSerpentError, "byte limit"):
                    load_tracklist(path)
                decoder.assert_not_called()

    def test_json_and_text_stop_at_the_track_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "tracks.json"
            json_path.write_text(
                json.dumps({"tracks": [{"title": "Track"}] * (MAX_TRACKS + 1)}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(GrooveSerpentError, "more than"):
                load_tracklist(json_path)

            text_path = Path(directory) / "tracks.txt"
            text_path.write_text("Track\n" * (MAX_TRACKS + 1), encoding="utf-8")
            with self.assertRaisesRegex(GrooveSerpentError, "more than"):
                load_tracklist(text_path)

    def test_oversized_fields_and_lines_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tracks.txt"
            path.write_text("X" * (MAX_TRACKLIST_LINE_BYTES + 1), encoding="utf-8")
            with self.assertRaisesRegex(GrooveSerpentError, "line exceeds"):
                load_tracklist(path)

            json_path = Path(directory) / "tracks.json"
            json_path.write_text(json.dumps([{"title": "X" * 4097}]), encoding="utf-8")
            with self.assertRaisesRegex(GrooveSerpentError, "Track title exceeds"):
                load_tracklist(json_path)

    def test_json_trailing_comma_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tracks.json"
            path.write_text('{"tracks":["Track",]}', encoding="utf-8")
            with self.assertRaisesRegex(GrooveSerpentError, "Invalid JSON"):
                load_tracklist(path)


if __name__ == "__main__":
    unittest.main()
