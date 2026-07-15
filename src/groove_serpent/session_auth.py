"""Per-process capability authentication for loopback review servers."""

from __future__ import annotations

import hmac
import re
import secrets
import threading
from email.message import Message
from typing import Literal


_BOOTSTRAP_PREFIX = "/__groove_serpent_session__/"
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
SessionAuthentication = Literal["bearer", "cookie"]


def request_target_is_exact(requestline: str, parsed_target: str) -> bool:
    """Reject request-targets normalized by the standard-library parser.

    ``BaseHTTPRequestHandler`` intentionally collapses two or more leading
    slashes before exposing ``path``. Authorization routes require one exact
    target, so compare it with the original token retained in ``requestline``.
    """

    words = requestline.split()
    return len(words) in {2, 3} and words[1] == parsed_target


def _strict_cookie_values(value: str) -> dict[str, str] | None:
    """Parse a Cookie header without accepting duplicate or malformed fields."""

    if not value or not value.isascii() or "\r" in value or "\n" in value:
        return None
    parsed: dict[str, str] = {}
    for rendered in value.split(";"):
        item = rendered.strip()
        if not item or "=" not in item:
            return None
        name, cookie_value = item.split("=", 1)
        if (
            not _COOKIE_NAME.fullmatch(name)
            or name in parsed
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in cookie_value)
        ):
            return None
        if cookie_value.startswith('"') or cookie_value.endswith('"'):
            if len(cookie_value) < 2 or not (
                cookie_value.startswith('"') and cookie_value.endswith('"')
            ):
                return None
            cookie_value = cookie_value[1:-1]
            if '"' in cookie_value or "\\" in cookie_value:
                return None
        parsed[name] = cookie_value
    return parsed


class LoopbackSessionAuth:
    """Own one unguessable bearer capability and one uniquely named browser cookie."""

    __slots__ = (
        "_bootstrap_consumed",
        "_bootstrap_lock",
        "_bootstrap_path",
        "_cookie_name",
        "_public_host",
        "_token",
    )

    def __init__(self) -> None:
        # token_urlsafe(32) draws 32 random bytes: a 256-bit capability.
        self._token = secrets.token_urlsafe(32)
        self._bootstrap_lock = threading.Lock()
        self._bootstrap_consumed = False
        self._cookie_name = f"groove_serpent_{secrets.token_hex(16)}"
        # Cookies are scoped to hosts, not ports. Give every server an
        # unguessable RFC-safe localhost name so another loopback service does
        # not receive this server's browser capability.
        self._public_host = f"groove-serpent-{secrets.token_hex(16)}.localhost"
        self._bootstrap_path = self._new_bootstrap_path()

    def _new_bootstrap_path(self) -> str:
        bootstrap_nonce = secrets.token_urlsafe(32)
        while hmac.compare_digest(bootstrap_nonce, self._token):
            bootstrap_nonce = secrets.token_urlsafe(32)
        return f"{_BOOTSTRAP_PREFIX}{bootstrap_nonce}"

    def __repr__(self) -> str:
        return "LoopbackSessionAuth(<redacted>)"

    @property
    def authorization_header(self) -> str:
        """Return the capability in the supported native-client header form."""

        return f"Bearer {self._token}"

    @property
    def bootstrap_path(self) -> str:
        """Return the one exact URL path that may establish the browser session."""

        with self._bootstrap_lock:
            return self._bootstrap_path

    @property
    def set_cookie_header(self) -> str:
        """Return a host-only, per-server, non-script-readable session cookie."""

        return (
            f"{self._cookie_name}={self._token}; Path=/; HttpOnly; "
            "SameSite=Strict"
        )

    @property
    def public_host(self) -> str:
        """Return this server's independent, browser-resolvable loopback host."""

        return self._public_host

    def origin(self, *, port: int) -> str:
        """Build this server's exact browser origin."""

        if type(port) is not int or not 1 <= port <= 65_535:
            raise ValueError("Loopback session port must be an integer from 1 to 65535.")
        return f"http://{self._public_host}:{port}"

    def bootstrap_url(self, *, port: int) -> str:
        """Build the secret-bearing URL passed directly to the user's browser."""

        return f"{self.origin(port=port)}{self.bootstrap_path}"

    def is_bootstrap_target(self, path: str) -> bool:
        """Match the secret bootstrap route without token-dependent timing."""

        if not path.isascii():
            return False
        with self._bootstrap_lock:
            return hmac.compare_digest(path, self._bootstrap_path)

    def consume_bootstrap(self, path: str) -> bool:
        """Atomically consume the exact bootstrap nonce once."""

        if not path.isascii():
            return False
        with self._bootstrap_lock:
            if (
                self._bootstrap_consumed
                or not hmac.compare_digest(path, self._bootstrap_path)
            ):
                return False
            self._bootstrap_consumed = True
            return True

    def rearm_bootstrap_if_consumed(self) -> None:
        """Issue a fresh one-time nonce after an owned child launch consumed its prior one."""

        with self._bootstrap_lock:
            if self._bootstrap_consumed:
                self._bootstrap_path = self._new_bootstrap_path()
                self._bootstrap_consumed = False

    def authentication_method(self, headers: Message) -> SessionAuthentication | None:
        """Return the strict credential form that authenticated this request."""

        authorization_values = headers.get_all("Authorization", [])
        cookie_values = headers.get_all("Cookie", [])
        if len(authorization_values) > 1 or len(cookie_values) > 1:
            return None

        cookie_authenticated = False
        if cookie_values:
            parsed_cookies = _strict_cookie_values(cookie_values[0])
            if parsed_cookies is None:
                return None
            candidate = parsed_cookies.get(self._cookie_name)
            cookie_authenticated = (
                candidate is not None
                and candidate.isascii()
                and hmac.compare_digest(candidate, self._token)
            )

        if not authorization_values:
            return "cookie" if cookie_authenticated else None
        authorization = authorization_values[0]
        if not authorization.isascii() or authorization != authorization.strip():
            return None
        pieces = authorization.split(" ")
        if (
            len(pieces) != 2
            or pieces[0].casefold() != "bearer"
            or not pieces[1]
            or any(character.isspace() for character in pieces[1])
        ):
            return None
        return "bearer" if hmac.compare_digest(pieces[1], self._token) else None

    def authenticated(self, headers: Message) -> bool:
        """Authenticate one strict Bearer header or this server's strict cookie."""

        return self.authentication_method(headers) is not None
