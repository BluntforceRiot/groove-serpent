from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any, Mapping

from groove_serpent import __user_agent__
from groove_serpent.metadata import (
    CoverArtArchiveClient,
    DEFAULT_USER_AGENT,
    MetadataLookupError,
    MusicBrainzClient,
    find_track_selections,
)


RELEASE_ID = "11111111-1111-4111-8111-111111111111"
RELEASE_GROUP_ID = "22222222-2222-4222-8222-222222222222"


class FakeResponse(io.BytesIO):
    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        final_url: str = "https://coverartarchive.org/release/example/front",
    ) -> None:
        super().__init__(payload)
        self.status = status
        self.headers = dict(headers or {})
        self._final_url = final_url

    def geturl(self) -> str:
        return self._final_url


class FixtureMusicBrainzClient(MusicBrainzClient):
    def __init__(self, responses: list[Any]) -> None:
        super().__init__(user_agent="GrooveSerpentTests/1.0 (offline fixtures)")
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _request_json(self, path: str, params: Mapping[str, Any]) -> Any:
        self.calls.append((path, dict(params)))
        return self.responses.pop(0)


class MetadataTests(unittest.TestCase):
    def test_default_network_identity_has_public_contact_url(self) -> None:
        self.assertEqual(
            DEFAULT_USER_AGENT,
            __user_agent__,
        )
        self.assertTrue(DEFAULT_USER_AGENT.startswith("GrooveSerpent/"))
        self.assertIn("https://github.com/BluntforceRiot/groove-serpent", DEFAULT_USER_AGENT)

    def test_search_releases_prefers_vinyl_and_simplifies_fields(self) -> None:
        response = {
            "releases": [
                {
                    "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "title": "Needle Dreams",
                    "artist-credit": [{"name": "The Coils", "joinphrase": ""}],
                    "date": "1999-04-01",
                    "country": "US",
                    "score": "100",
                    "status": "Official",
                    "media": [{"format": "CD", "track-count": 10}],
                    "barcode": "012345678905",
                    "label-info": [
                        {
                            "catalog-number": "CD-10",
                            "label": {"name": "Round Records"},
                        }
                    ],
                    "release-group": {"id": RELEASE_GROUP_ID},
                    "cover-art-archive": {"artwork": False, "front": False},
                },
                {
                    "id": RELEASE_ID,
                    "title": "Needle Dreams",
                    "artist-credit": [
                        {"name": "The Coils", "joinphrase": " feat. "},
                        {"artist": {"name": "Guest"}, "joinphrase": ""},
                    ],
                    "date": "1999",
                    "country": "GB",
                    "score": 91,
                    "status": "Official",
                    "media": [
                        {"format": '12\" Vinyl', "track-count": 4},
                        {"format": "Vinyl", "track-count": 4},
                    ],
                    "barcode": None,
                    "label-info": [
                        {
                            "catalog-number": "RR 42",
                            "label": {"name": "Round Records"},
                        }
                    ],
                    "release-group": {"id": RELEASE_GROUP_ID},
                    "cover-art-archive": {
                        "artwork": True,
                        "front": True,
                        "count": 1,
                    },
                },
            ]
        }
        client = FixtureMusicBrainzClient([response])

        results = client.search_releases('The "Coils"', "Needle Dreams", limit=7)

        self.assertEqual(results[0]["id"], RELEASE_ID)
        self.assertEqual(results[0]["artist"], "The Coils feat. Guest")
        self.assertEqual(results[0]["formats"], ['12" Vinyl', "Vinyl"])
        self.assertEqual(results[0]["track_count"], 8)
        self.assertEqual(results[0]["label"], "Round Records")
        self.assertEqual(results[0]["catalog_number"], "RR 42")
        self.assertEqual(results[0]["release_group_id"], RELEASE_GROUP_ID)
        self.assertTrue(results[0]["has_artwork"])
        self.assertEqual(results[1]["score"], 100)
        path, params = client.calls[0]
        self.assertEqual(path, "release/")
        self.assertEqual(params["limit"], 7)
        self.assertIn(r'artist:"The \"Coils\""', params["query"])

    def test_search_validation_is_user_facing(self) -> None:
        client = FixtureMusicBrainzClient([])
        with self.assertRaisesRegex(MetadataLookupError, "Artist and album"):
            client.search_releases("", "Album")
        with self.assertRaisesRegex(MetadataLookupError, "between 1 and 100"):
            client.search_releases("Artist", "Album", 101)

    def test_get_release_groups_vinyl_sides_and_preserves_track_ids(self) -> None:
        response = {
            "id": RELEASE_ID,
            "title": "Needle Dreams",
            "artist-credit": [{"name": "The Coils", "joinphrase": ""}],
            "date": "1999-04-01",
            "country": "GB",
            "status": "Official",
            "barcode": "012345678905",
            "label-info": [
                {
                    "catalog-number": "RR 42",
                    "label": {"name": "Round Records"},
                }
            ],
            "release-group": {
                "id": RELEASE_GROUP_ID,
                "genres": [{"name": "dream pop", "count": 3}],
            },
            "genres": [{"name": "shoegaze", "count": 2}],
            "cover-art-archive": {
                "artwork": True,
                "front": True,
                "back": False,
                "count": 1,
            },
            "media": [
                {
                    "position": 1,
                    "format": '12\" Vinyl',
                    "tracks": [
                        {
                            "id": "track-a1",
                            "position": 1,
                            "number": "A1",
                            "title": "Coiled Light",
                            "length": 201234,
                            "recording": {"id": "recording-a1"},
                        },
                        {
                            "id": "track-a2",
                            "position": 2,
                            "number": "A02",
                            "recording": {
                                "id": "recording-a2",
                                "title": "Velvet Static",
                                "length": 189000,
                                "artist-credit": [
                                    {"name": "The Coils", "joinphrase": " & "},
                                    {"name": "Mira", "joinphrase": ""},
                                ],
                            },
                        },
                        {
                            "id": "track-b1",
                            "position": 3,
                            "number": "B-1",
                            "title": "Turnover",
                            "length": 242000,
                            "recording": {"id": "recording-b1"},
                        },
                    ],
                },
                {
                    "position": 2,
                    "title": "Bonus disc",
                    "format": "CD",
                    "tracks": [
                        {
                            "id": "track-1",
                            "position": 1,
                            "number": "1",
                            "title": "Hidden Coil",
                            "recording": {"id": "recording-1"},
                        }
                    ],
                },
            ],
        }
        client = FixtureMusicBrainzClient([response])

        details = client.get_release(RELEASE_ID.upper())

        self.assertEqual(client.calls[0][0], f"release/{RELEASE_ID}")
        self.assertEqual(
            client.calls[0][1]["inc"],
            "recordings+artist-credits+release-groups+genres+labels",
        )
        self.assertEqual(details["label"], "Round Records")
        self.assertEqual(details["catalog_number"], "RR 42")
        self.assertEqual(details["genres"], ["shoegaze", "dream pop"])
        self.assertEqual(details["formats"], ['12" Vinyl', "CD"])
        self.assertTrue(details["artwork"]["available"])
        self.assertEqual(
            details["artwork"]["metadata_url"],
            f"https://coverartarchive.org/release/{RELEASE_ID}",
        )
        sides = details["media"][0]["sides"]
        self.assertEqual([side["side"] for side in sides], ["A", "B"])
        self.assertEqual([side["track_count"] for side in sides], [2, 1])
        second = sides[0]["tracks"][1]
        self.assertEqual(second["side_position"], 2)
        self.assertEqual(second["artist"], "The Coils & Mira")
        self.assertEqual(second["recording_id"], "recording-a2")
        self.assertEqual(second["track_id"], "track-a2")
        self.assertEqual(second["duration_ms"], 189000)
        self.assertEqual(second["duration_seconds"], 189.0)
        self.assertEqual(
            [choice["key"] for choice in details["selections"]],
            [
                "medium:1:side:A",
                "medium:1:side:B",
                "medium:1:all",
                "medium:2:all",
                "release:all",
            ],
        )
        complete = details["selections"][-1]
        self.assertEqual(complete["track_count"], 4)
        self.assertEqual(
            [track.get("side", "") for track in complete["tracks"]],
            ["A", "A", "B", ""],
        )
        self.assertEqual(
            find_track_selections(details, expected_count=4)[0]["key"],
            "release:all",
        )

        ranked = find_track_selections(details, preferred_side="Side B", expected_count=1)
        self.assertEqual(ranked[0]["key"], "medium:1:side:B")
        count_ranked = find_track_selections(details, preferred_side="B", expected_count=2)
        self.assertEqual(count_ranked[0]["key"], "medium:1:side:A")

    def test_get_release_rejects_invalid_uuid_before_request(self) -> None:
        client = FixtureMusicBrainzClient([])
        with self.assertRaisesRegex(MetadataLookupError, "valid MusicBrainz UUID"):
            client.get_release("../../not-a-release")
        self.assertEqual(client.calls, [])

    def test_find_track_selections_validates_expected_count(self) -> None:
        with self.assertRaisesRegex(MetadataLookupError, "positive integer"):
            find_track_selections({"selections": []}, expected_count=0)

    def test_musicbrainz_invalid_json_and_http_errors_are_wrapped(self) -> None:
        class DirectClient(MusicBrainzClient):
            def __init__(self, response: Any) -> None:
                super().__init__(
                    user_agent="GrooveSerpentTests/1.0 (offline fixtures)",
                    base_url="https://fixtures.invalid/ws/2",
                )
                self.response = response

            def _wait_for_rate_limit(self) -> None:
                pass

            def _open(self, request: Any) -> Any:
                if isinstance(self.response, BaseException):
                    raise self.response
                return self.response

        with self.assertRaisesRegex(MetadataLookupError, "invalid JSON"):
            DirectClient(FakeResponse(b"not-json"))._request_json("release/", {})

        http_error = urllib.error.HTTPError(
            "https://fixtures.invalid",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b"try later"),
        )
        with self.assertRaisesRegex(MetadataLookupError, "HTTP 503"):
            DirectClient(http_error)._request_json("release/", {})

    def test_musicbrainz_user_agent_header_and_shared_rate_limit(self) -> None:
        class HeaderClient(MusicBrainzClient):
            def __init__(self) -> None:
                super().__init__(
                    user_agent="MyVinylTool/2.4 (owner@example.test)",
                    base_url="https://headers.invalid/ws/2",
                )
                self.request: Any = None

            def _wait_for_rate_limit(self) -> None:
                pass

            def _open(self, request: Any) -> FakeResponse:
                self.request = request
                return FakeResponse(json.dumps({"releases": []}).encode())

        header_client = HeaderClient()
        header_client.search_releases("Artist", "Album")
        self.assertEqual(
            header_client.request.get_header("User-agent"),
            "MyVinylTool/2.4 (owner@example.test)",
        )

        class ClockClient(MusicBrainzClient):
            clock = 100.0
            sleeps: list[float] = []

            def _monotonic(self) -> float:
                return ClockClient.clock

            def _sleep(self, seconds: float) -> None:
                ClockClient.sleeps.append(seconds)
                ClockClient.clock += seconds

        first = ClockClient(base_url="https://rate-fixture.invalid/ws/2")
        second = ClockClient(base_url="https://rate-fixture.invalid/ws/2")
        MusicBrainzClient._next_request_by_origin.pop(first._rate_origin, None)
        first._wait_for_rate_limit()
        second._wait_for_rate_limit()
        self.assertEqual(ClockClient.sleeps, [1.0])


class CoverArtTests(unittest.TestCase):
    def test_resolve_front_art_prefers_approved_and_normalizes_https(self) -> None:
        payload = {
            "images": [
                {
                    "id": 1,
                    "front": True,
                    "approved": False,
                    "image": "http://coverartarchive.org/release/unapproved.jpg",
                    "thumbnails": {
                        "500": "http://coverartarchive.org/release/unapproved-500.jpg"
                    },
                },
                {
                    "id": 2,
                    "front": True,
                    "approved": True,
                    "comment": "Primary sleeve",
                    "image": "http://coverartarchive.org/release/approved.jpg",
                    "thumbnails": {
                        "1200": "https://coverartarchive.org/release/approved-1200.jpg",
                        "500": "https://coverartarchive.org/release/approved-500.jpg",
                    },
                },
            ]
        }

        class ResolveClient(CoverArtArchiveClient):
            def _request_json(self, path: str) -> Any:
                self.path = path
                return payload

        with tempfile.TemporaryDirectory() as directory:
            client = ResolveClient(directory)
            result = client.resolve_front_art(RELEASE_ID)

        self.assertEqual(client.path, f"release/{RELEASE_ID}")
        self.assertEqual(result["artwork_id"], "2")
        self.assertTrue(result["approved"])
        self.assertEqual(result["comment"], "Primary sleeve")
        self.assertEqual(
            result["source_url"],
            "https://coverartarchive.org/release/approved.jpg",
        )
        self.assertIn("1200", result["urls"])

    def test_resolve_release_group_front_art_uses_group_endpoint(self) -> None:
        payload = {
            "images": [
                {
                    "id": 7,
                    "front": True,
                    "approved": True,
                    "image": "http://coverartarchive.org/release/group-front.jpg",
                    "thumbnails": {
                        "500": "http://coverartarchive.org/release/group-front-500.jpg"
                    },
                }
            ]
        }

        class GroupClient(CoverArtArchiveClient):
            def _request_json(self, path: str) -> Any:
                self.path = path
                return payload

        with tempfile.TemporaryDirectory() as directory:
            client = GroupClient(directory)
            result = client.resolve_release_group_front_art(RELEASE_GROUP_ID.upper())

        self.assertEqual(client.path, f"release-group/{RELEASE_GROUP_ID}")
        self.assertEqual(result["release_group_id"], RELEASE_GROUP_ID)
        self.assertEqual(result["storage_id"], RELEASE_GROUP_ID)
        self.assertEqual(
            result["source_url"],
            "https://coverartarchive.org/release/group-front.jpg",
        )

    def test_download_release_group_art_uses_group_id_filename(self) -> None:
        image = b"\xff\xd8\xff" + b"release-group-cover"
        payload = {
            "images": [
                {
                    "id": 8,
                    "front": True,
                    "approved": True,
                    "image": "https://coverartarchive.org/release/group-original.jpg",
                    "thumbnails": {
                        "500": "https://coverartarchive.org/release/group-500.jpg"
                    },
                }
            ]
        }

        class GroupDownloadClient(CoverArtArchiveClient):
            def _request_json(self, path: str) -> Any:
                self.path = path
                return payload

            def _open(self, request: Any) -> FakeResponse:
                return FakeResponse(
                    image,
                    headers={
                        "Content-Type": "image/jpeg",
                        "Content-Length": str(len(image)),
                    },
                    final_url="https://archive.org/download/group/cover.jpg",
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = GroupDownloadClient(root)
            result = client.download_release_group_front_art(
                RELEASE_GROUP_ID, size="500"
            )
            saved = root / result["relative_path"]
            self.assertEqual(saved.read_bytes(), image)

        self.assertEqual(client.path, f"release-group/{RELEASE_GROUP_ID}")
        self.assertEqual(
            result["relative_path"],
            f"artwork/{RELEASE_GROUP_ID}-front-500.jpg",
        )

    def test_download_front_art_is_atomic_bounded_and_hashes_result(self) -> None:
        image = b"\xff\xd8\xff" + b"fixture-jpeg-payload"

        class DownloadClient(CoverArtArchiveClient):
            def resolve_front_art(self, release_id: str) -> dict[str, Any]:
                return {
                    "release_id": RELEASE_ID,
                    "artwork_id": "2",
                    "urls": {
                        "original": "https://coverartarchive.org/release/original.jpg",
                        "1200": "https://coverartarchive.org/release/front-1200.jpg",
                    },
                }

            def _open(self, request: Any) -> FakeResponse:
                self.request = request
                return FakeResponse(
                    image,
                    headers={
                        "Content-Type": "image/jpeg; charset=binary",
                        "Content-Length": str(len(image)),
                    },
                    final_url="https://ia801.example.archive.org/cover.jpg",
                )

        with tempfile.TemporaryDirectory(prefix="Groove Sérpent ") as directory:
            root = Path(directory)
            result = DownloadClient(root).download_front_art(RELEASE_ID, size="1200")
            saved = root / result["relative_path"]
            self.assertTrue(saved.is_file())
            self.assertEqual(saved.read_bytes(), image)
            self.assertEqual(list((root / "artwork").glob(".cover-*.tmp")), [])

        self.assertEqual(result["mime_type"], "image/jpeg")
        self.assertEqual(result["size_bytes"], len(image))
        self.assertEqual(result["sha256"], hashlib.sha256(image).hexdigest())
        self.assertEqual(result["selected_size"], "1200")
        self.assertTrue(result["relative_path"].endswith("-front-1200.jpg"))

    def test_download_rejects_unsupported_types_and_cleans_partial_file(self) -> None:
        class BadDownloadClient(CoverArtArchiveClient):
            def __init__(self, root: Path, payload: bytes, content_type: str) -> None:
                super().__init__(root, max_bytes=8)
                self.payload = payload
                self.content_type = content_type

            def resolve_front_art(self, release_id: str) -> dict[str, Any]:
                return {
                    "release_id": RELEASE_ID,
                    "urls": {
                        "original": "https://coverartarchive.org/release/original.jpg"
                    },
                }

            def _open(self, request: Any) -> FakeResponse:
                return FakeResponse(
                    self.payload,
                    headers={"Content-Type": self.content_type},
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(MetadataLookupError, "JPEG or PNG"):
                BadDownloadClient(root, b"text", "text/html").download_front_art(
                    RELEASE_ID, size="original"
                )
            with self.assertRaisesRegex(MetadataLookupError, "download limit"):
                BadDownloadClient(
                    root, b"\xff\xd8\xff123456", "image/jpeg"
                ).download_front_art(RELEASE_ID, size="original")
            artwork = root / "artwork"
            self.assertEqual(list(artwork.iterdir()) if artwork.exists() else [], [])

    def test_resolve_rejects_unsafe_image_url(self) -> None:
        class UnsafeClient(CoverArtArchiveClient):
            def _request_json(self, path: str) -> Any:
                return {
                    "images": [
                        {
                            "front": True,
                            "approved": True,
                            "image": "https://example.invalid/tracker.jpg",
                        }
                    ]
                }

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MetadataLookupError, "unsafe image URL"):
                UnsafeClient(directory).resolve_front_art(RELEASE_ID)


if __name__ == "__main__":
    unittest.main()
