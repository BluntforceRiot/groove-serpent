"""Bounded HTTP response capture for opt-in online providers."""

from __future__ import annotations

import math
import queue
import socket
import threading
import time
import urllib.error
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, cast


_MAX_ACTIVE_PROVIDER_REQUESTS = 4
_PROVIDER_REQUEST_SLOTS = threading.BoundedSemaphore(_MAX_ACTIVE_PROVIDER_REQUESTS)


class ProviderDeadlineError(TimeoutError):
    """A provider did not complete before its end-to-end deadline."""


class ProviderConcurrencyError(OSError):
    """All bounded provider request workers are occupied."""


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """Immutable response metadata and a size-bounded body."""

    status: int
    reason: str
    url: str
    headers: Mapping[str, str]
    body: bytes

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.casefold(), default)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderDeadlineError("Provider request exceeded its total deadline.")
    return remaining


def _response_socket(response: Any) -> socket.socket | None:
    pending: list[Any] = [response]
    visited: set[int] = set()
    while pending:
        candidate = pending.pop()
        identity = id(candidate)
        if identity in visited:
            continue
        visited.add(identity)
        if isinstance(candidate, socket.socket):
            return candidate
        for attribute in ("fp", "raw", "_sock", "sock", "socket", "_fp"):
            try:
                nested = getattr(candidate, attribute, None)
            except (AttributeError, OSError, ValueError):
                continue
            if nested is not None:
                pending.append(nested)
    return None


def _set_response_timeout(response: Any, timeout: float) -> None:
    active_socket = _response_socket(response)
    if active_socket is None:
        return
    try:
        active_socket.settimeout(timeout)
    except OSError:
        pass


def _abort_response(response: Any) -> None:
    active_socket = _response_socket(response)
    if active_socket is None:
        return
    try:
        active_socket.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        active_socket.close()
    except OSError:
        pass


@contextmanager
def _managed_response(response: Any) -> Iterator[Any]:
    if callable(getattr(response, "__enter__", None)) and callable(
        getattr(response, "__exit__", None)
    ):
        with cast(AbstractContextManager[Any], response) as opened:
            yield opened
        return
    try:
        yield response
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except (OSError, ValueError):
                pass


def _snapshot_response(
    response: Any,
    deadline: float,
    max_bytes: int,
    validate_response: Callable[[ProviderResponse], None] | None,
) -> ProviderResponse:
    status = int(getattr(response, "status", getattr(response, "code", 200)))
    reason = str(getattr(response, "reason", ""))
    get_url = getattr(response, "geturl", None)
    url = str(get_url()) if callable(get_url) else ""
    raw_headers = getattr(response, "headers", None)
    headers: dict[str, str] = {}
    if raw_headers is not None:
        items = getattr(raw_headers, "items", None)
        if callable(items):
            headers = {str(key).casefold(): str(value) for key, value in items()}
    metadata = ProviderResponse(
        status=status,
        reason=reason,
        url=url,
        headers=headers,
        body=b"",
    )
    if validate_response is not None:
        validate_response(metadata)

    body_limit = max_bytes + 1
    remaining = _remaining(deadline)
    _set_response_timeout(response, remaining)
    body = response.read(body_limit)
    if not isinstance(body, (bytes, bytearray)):
        raise OSError("Provider response returned non-binary data.")
    _remaining(deadline)
    return ProviderResponse(
        status=status,
        reason=reason,
        url=url,
        headers=headers,
        body=bytes(body),
    )


def fetch_provider_response(
    open_response: Callable[[float], Any],
    *,
    timeout: float,
    max_bytes: int,
    validate_response: Callable[[ProviderResponse], None] | None = None,
) -> ProviderResponse:
    """Open and capture a provider response under one bounded wall-clock budget."""

    try:
        timeout_seconds = float(timeout)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timeout must be a positive finite number of seconds.") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout must be a positive finite number of seconds.")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer.")

    deadline = time.monotonic() + timeout_seconds
    if not _PROVIDER_REQUEST_SLOTS.acquire(blocking=False):
        raise ProviderConcurrencyError("Provider request concurrency limit reached.")

    outcomes: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
    cancelled = threading.Event()
    active_lock = threading.Lock()
    active_response: list[Any | None] = [None]

    def perform() -> ProviderResponse:
        try:
            response = open_response(_remaining(deadline))
        except urllib.error.HTTPError as exc:
            response = exc
        with _managed_response(response) as opened:
            with active_lock:
                active_response[0] = opened
            try:
                if cancelled.is_set():
                    raise ProviderDeadlineError(
                        "Provider request exceeded its total deadline."
                    )
                return _snapshot_response(
                    opened,
                    deadline,
                    max_bytes,
                    validate_response,
                )
            finally:
                with active_lock:
                    active_response[0] = None

    def worker() -> None:
        try:
            outcome: tuple[bool, object] = (True, perform())
        except BaseException as exc:
            outcome = (False, exc)
        finally:
            _PROVIDER_REQUEST_SLOTS.release()
        if not cancelled.is_set():
            try:
                outcomes.put_nowait(outcome)
            except queue.Full:
                pass

    thread = threading.Thread(
        target=worker,
        name="groove-serpent-provider-request",
        daemon=True,
    )
    try:
        thread.start()
    except BaseException:
        _PROVIDER_REQUEST_SLOTS.release()
        raise

    def cancel_active_response() -> None:
        cancelled.set()
        with active_lock:
            response_to_abort = active_response[0]
        if response_to_abort is not None:
            _abort_response(response_to_abort)

    try:
        wait_timeout = _remaining(deadline)
    except ProviderDeadlineError:
        cancel_active_response()
        raise
    try:
        succeeded, value = outcomes.get(timeout=wait_timeout)
    except queue.Empty as exc:
        cancel_active_response()
        raise ProviderDeadlineError(
            "Provider request exceeded its total deadline."
        ) from exc
    if time.monotonic() >= deadline:
        cancel_active_response()
        raise ProviderDeadlineError("Provider request exceeded its total deadline.")
    if succeeded:
        return cast(ProviderResponse, value)
    if not isinstance(value, BaseException):
        raise AssertionError("Provider worker returned an invalid failure.")
    raise value


__all__ = [
    "ProviderConcurrencyError",
    "ProviderDeadlineError",
    "ProviderResponse",
    "fetch_provider_response",
]
