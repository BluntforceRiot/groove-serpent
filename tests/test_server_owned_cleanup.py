from __future__ import annotations

import hashlib
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import test_album_review_server as album_fixture
import test_review_server as side_fixture
from groove_serpent import album_review_server as album_server
from groove_serpent import review_server as side_server
from groove_serpent.atomic_create import capture_owned_file_receipt
from groove_serpent.metadata import ArtworkDownloadResult
from groove_serpent.errors import ProjectValidationError


class ServerRejectedBodyTests(unittest.TestCase):
    def test_unauthorized_delayed_body_is_drained_before_response(self) -> None:
        for fixture_type, handler_type in (
            (side_fixture.ReviewServerTests, side_server.ReviewHandler),
            (album_fixture.AlbumReviewServerTests, album_server.AlbumReviewHandler),
        ):
            with self.subTest(server=handler_type.__name__):
                fixture = fixture_type()
                fixture.setUp()
                try:
                    entered = threading.Event()
                    original = handler_type._discard_declared_request_body

                    def observed(handler):
                        entered.set()
                        original(handler)

                    with patch.object(handler_type, "_discard_declared_request_body", observed):
                        for _ in range(20):
                            entered.clear()
                            connection = http.client.HTTPConnection(
                                "127.0.0.1", fixture.port, timeout=2
                            )
                            try:
                                connection.putrequest("POST", "/api/save", skip_host=True)
                                connection.putheader("Host", fixture.authority)
                                connection.putheader("Content-Length", "2")
                                connection.endheaders()
                                self.assertTrue(entered.wait(1), "Server never began bounded drain")
                                time.sleep(0.01)
                                connection.send(b"{}")
                                response = connection.getresponse()
                                body = response.read()
                                self.assertEqual(response.status, 401, body)
                                self.assertIsNotNone(response.headers.get("WWW-Authenticate"))
                                self.assertTrue(response.will_close)
                            finally:
                                connection.close()
                finally:
                    fixture.tearDown()

    def test_incomplete_or_unbounded_unauthorized_body_cannot_hang_server(self) -> None:
        for fixture_type in (
            side_fixture.ReviewServerTests, album_fixture.AlbumReviewServerTests
        ):
            fixture = fixture_type()
            fixture.setUp()
            try:
                for headers in (
                    (("Content-Length", "2"),),
                    (("Content-Length", "65537"),),
                    (("Content-Length", "9" * 100),),
                    (("Content-Length", "2"), ("Content-Length", "2")),
                    (("Transfer-Encoding", "chunked"),),
                ):
                    with self.subTest(server=fixture_type.__name__, headers=headers):
                        connection = http.client.HTTPConnection(
                            "127.0.0.1", fixture.port, timeout=2
                        )
                        try:
                            connection.putrequest("POST", "/api/save", skip_host=True)
                            connection.putheader("Host", fixture.authority)
                            for key, value in headers:
                                connection.putheader(key, value)
                            started = time.monotonic()
                            connection.endheaders()
                            response = connection.getresponse()
                            body = response.read()
                            self.assertEqual(response.status, 401, body)
                            self.assertLess(time.monotonic() - started, 1.5)
                            self.assertTrue(response.will_close)
                        finally:
                            connection.close()
            finally:
                fixture.tearDown()


class ServerOwnedCleanupTests(unittest.TestCase):
    def test_growing_artwork_reader_stops_at_declared_size(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = root / "artwork" / "review" / "cover.jpg"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"original")
            original_open = Path.open
            requests = []

            @contextmanager
            def growing_open(candidate, *args, **kwargs):
                with original_open(candidate, *args, **kwargs) as handle:
                    class GrowingFile:
                        def fileno(self):
                            return handle.fileno()

                        def read(self, size):
                            requests.append(size)
                            if len(requests) > 2:
                                raise AssertionError("Unbounded artwork read")
                            return b"x" * size

                    yield GrowingFile()

            with patch.object(Path, "open", growing_open):
                with self.assertRaisesRegex(ProjectValidationError, "grew beyond"):
                    album_server._safe_review_artwork_path(
                        root / "album.json", relative_path="artwork/review/cover.jpg",
                        expected_sha256=hashlib.sha256(b"original").hexdigest(), expected_size=8,
                    )
            self.assertEqual(requests, [9])

    def test_allocating_restoration_name_does_not_authorize_deleting_occupant(self) -> None:
        fixture = side_fixture.ReviewServerTests()
        fixture.setUp()
        try:
            for directory in (False, True):
                with self.subTest(directory=directory):
                    path = fixture.server.new_restoration_path("preview" if directory else "scan")
                    if directory:
                        path.mkdir()
                        (path / "foreign.txt").write_bytes(b"foreign")
                    else:
                        path.write_bytes(b"foreign")
                    fixture.server.discard_restoration_path(path)
                    self.assertTrue(path.exists())
                    self.assertEqual(fixture.server.restoration_artifacts, {})
        finally:
            fixture.tearDown()

    def test_artwork_cleanup_requires_original_writer_receipt(self) -> None:
        for replacement in (None, b"same image", b"different image"):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                path = root / "artwork" / "review" / "cover.jpg"
                path.parent.mkdir(parents=True)
                payload = b"same image"
                with path.open("xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                    receipt = capture_owned_file_receipt(
                        path, payload, owned_descriptor=handle.fileno()
                    )
                artwork = {
                    "relative_path": "artwork/review/cover.jpg",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                }
                self.assertFalse(album_server._discard_exact_review_artwork(root / "a", artwork))
                self.assertTrue(path.exists())
                if replacement is not None:
                    path.rename(path.with_suffix(".retained"))
                    path.write_bytes(replacement)
                removed = album_server._discard_exact_review_artwork(root / "a", artwork, receipt)
                self.assertEqual(removed, replacement is None)
                if replacement is not None:
                    self.assertEqual(path.read_bytes(), replacement)
                else:
                    self.assertFalse(path.exists())

    def test_artwork_normalization_drops_cleanup_payload_without_changing_schema(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "cover.jpg"
            payload = b"same image"
            with path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                receipt = capture_owned_file_receipt(
                    path, payload, owned_descriptor=handle.fileno()
                )
            values = {
                "relative_path": "artwork/review/cover.jpg",
                "source_url": "https://coverartarchive.org/release/front.jpg",
                "mime_type": "image/jpeg",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
                "requested_size": "1200",
                "selected_size": "1200",
            }
            result = ArtworkDownloadResult(values, cleanup_receipt=receipt)
            normalized = album_server._normalize_artwork_download(result)
            self.assertIs(type(normalized), dict)
            self.assertFalse(hasattr(normalized, "cleanup_receipt"))
            self.assertEqual(normalized, values)
            self.assertEqual(json.loads(json.dumps(result)), values)
