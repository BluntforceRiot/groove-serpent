from __future__ import annotations

import importlib
import unittest

from groove_serpent import album_publication_executor, review_server


class ReviewFixtureProbeIsolationTests(unittest.TestCase):
    def test_borrowed_fixture_teardown_restores_native_probe(self) -> None:
        fixtures = (
            ("test_review_server", "ReviewServerTests"),
            ("test_restoration_server", "RestorationServerTests"),
            ("test_endpoint_review_workflow", "EndpointReviewServerTests"),
            ("test_album_review_server", "AlbumReviewServerTests"),
        )
        original = review_server.probe_audio
        original_publication_probe = album_publication_executor.probe_audio
        for module_name, class_name in fixtures:
            with self.subTest(fixture=class_name):
                fixture = getattr(importlib.import_module(module_name), class_name)()
                fixture.setUp()
                try:
                    self.assertIsNot(review_server.probe_audio, original)
                finally:
                    # Several focused suites borrow only setUp/tearDown, without
                    # unittest.run and its implicit doCleanups call.
                    fixture.tearDown()
                self.assertIs(review_server.probe_audio, original)
                self.assertIs(album_publication_executor.probe_audio, original_publication_probe)
                fixture.doCleanups()
                self.assertIs(review_server.probe_audio, original)
                self.assertIs(album_publication_executor.probe_audio, original_publication_probe)
