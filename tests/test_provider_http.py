from __future__ import annotations

import io
import threading
import unittest

from groove_serpent.provider_http import (
    ProviderDeadlineError,
    fetch_provider_response,
)


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, *, closed: threading.Event | None = None) -> None:
        super().__init__(payload)
        self.status = 200
        self.reason = "OK"
        self.headers = {"Content-Type": "application/json"}
        self._closed_event = closed

    def geturl(self) -> str:
        return "https://provider.invalid/result"

    def close(self) -> None:
        super().close()
        if self._closed_event is not None:
            self._closed_event.set()


class _ObservedResponse(_Response):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.reads = 0

    def read(self, size: int = -1) -> bytes:
        self.reads += 1
        return super().read(size)


class _ShortReadResponse(_ObservedResponse):
    def read(self, size: int = -1) -> bytes:
        return super().read(min(size, 3))


class _TrickledBodyResponse:
    status = 200
    reason = "OK"
    headers = {"Content-Type": "application/octet-stream"}

    def __init__(
        self,
        started: threading.Event,
        release: threading.Event,
        closed: threading.Event,
    ) -> None:
        self._started = started
        self._release = release
        self._closed = closed

    def __enter__(self) -> _TrickledBodyResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self._closed.set()

    def geturl(self) -> str:
        return "https://provider.invalid/stalled"

    def read(self, size: int = -1) -> bytes:
        self._started.set()
        payload = bytearray()
        while size < 0 or len(payload) < size:
            if self._release.wait(timeout=0.01):
                break
            payload.extend(b"x")
        return bytes(payload)


class ProviderHTTPTests(unittest.TestCase):
    def test_successful_response_preserves_payload_and_metadata(self) -> None:
        observed_timeouts: list[float] = []

        def open_response(timeout: float) -> _Response:
            observed_timeouts.append(timeout)
            return _Response(b'{"ok":true}')

        response = fetch_provider_response(
            open_response,
            timeout=1.0,
            max_bytes=1024,
        )

        self.assertEqual(response.body, b'{"ok":true}')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.header("content-type"), "application/json")
        self.assertEqual(response.url, "https://provider.invalid/result")
        self.assertEqual(len(observed_timeouts), 1)
        self.assertGreater(observed_timeouts[0], 0)
        self.assertLessEqual(observed_timeouts[0], 1.0)

    def test_response_validation_precedes_body_read(self) -> None:
        response = _ObservedResponse(b"untrusted artwork")

        def reject_untrusted(_metadata: object) -> None:
            raise ValueError("unsafe response")

        with self.assertRaisesRegex(ValueError, "unsafe response"):
            fetch_provider_response(
                lambda _timeout: response,
                timeout=1.0,
                max_bytes=1024,
                validate_response=reject_untrusted,
            )

        self.assertEqual(response.reads, 0)

    def test_short_reads_are_collected_until_end_of_response(self) -> None:
        source = _ShortReadResponse(b'{"ok":true}')

        response = fetch_provider_response(
            lambda _timeout: source,
            timeout=1.0,
            max_bytes=1024,
        )

        self.assertEqual(response.body, b'{"ok":true}')
        self.assertGreater(source.reads, 2)

    def test_stalled_open_expires_total_deadline(self) -> None:
        started = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        late_response = _Response(b"late", closed=closed)
        guard = threading.Timer(1.0, release.set)

        def open_response(timeout: float) -> _Response:
            self.assertGreater(timeout, 0)
            started.set()
            release.wait()
            return late_response

        guard.start()
        try:
            with self.assertRaisesRegex(ProviderDeadlineError, "total deadline"):
                fetch_provider_response(
                    open_response,
                    timeout=0.05,
                    max_bytes=1024,
                )
            self.assertTrue(started.is_set())
        finally:
            release.set()
            guard.cancel()
        self.assertTrue(closed.wait(timeout=1.0))

    def test_trickled_body_expires_total_deadline(self) -> None:
        started = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        response = _TrickledBodyResponse(started, release, closed)
        guard = threading.Timer(1.0, release.set)

        guard.start()
        try:
            with self.assertRaisesRegex(ProviderDeadlineError, "total deadline"):
                fetch_provider_response(
                    lambda _timeout: response,
                    timeout=0.05,
                    max_bytes=1024,
                )
            self.assertTrue(started.is_set())
        finally:
            release.set()
            guard.cancel()
        self.assertTrue(closed.wait(timeout=1.0))


if __name__ == "__main__":
    unittest.main()
