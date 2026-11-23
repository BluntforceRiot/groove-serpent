"""HTTPS transport that validates and pins provider destinations before connecting."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from typing import IO, Any, BinaryIO, cast


_ResolvedAddress = tuple[int, int, int, tuple[Any, ...]]


class UnsafeNetworkTargetError(urllib.error.URLError):
    """A provider request attempted to leave its approved public HTTPS boundary."""


def _canonical_hostname(value: str) -> str:
    hostname = value.rstrip(".").casefold()
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeNetworkTargetError("Provider request contained an invalid hostname.") from exc
    if not hostname:
        raise UnsafeNetworkTargetError("Provider request did not contain a hostname.")
    return hostname


class HTTPSRequestPolicy:
    """Allowed HTTPS hosts and redirect bound for one provider request class."""

    __slots__ = ("exact_hosts", "subdomain_hosts", "max_redirects")

    exact_hosts: frozenset[str]
    subdomain_hosts: tuple[str, ...]
    max_redirects: int

    def __init__(
        self,
        exact_hosts: Iterable[str],
        *,
        subdomain_hosts: Iterable[str] = (),
        max_redirects: int = 5,
    ) -> None:
        self.exact_hosts = frozenset(_canonical_hostname(host) for host in exact_hosts)
        self.subdomain_hosts = tuple(
            _canonical_hostname(host) for host in subdomain_hosts
        )
        if not self.exact_hosts:
            raise ValueError("At least one exact HTTPS host is required.")
        if isinstance(max_redirects, bool) or not 1 <= max_redirects <= 10:
            raise ValueError("max_redirects must be between 1 and 10.")
        self.max_redirects = max_redirects

    def validate(self, value: str) -> tuple[str, int]:
        """Return the canonical host and port after applying URL policy."""

        try:
            parsed = urllib.parse.urlsplit(value)
            hostname = _canonical_hostname(parsed.hostname or "")
            port = parsed.port
        except (ValueError, UnsafeNetworkTargetError) as exc:
            if isinstance(exc, UnsafeNetworkTargetError):
                raise
            raise UnsafeNetworkTargetError("Provider request contained an invalid URL.") from exc
        if parsed.scheme.casefold() != "https" or not parsed.netloc:
            raise UnsafeNetworkTargetError("Provider requests require HTTPS.")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeNetworkTargetError("Provider request URLs may not contain credentials.")
        if port not in (None, 443):
            raise UnsafeNetworkTargetError("Provider requests require the standard HTTPS port.")
        trusted = hostname in self.exact_hosts or any(
            hostname.endswith(f".{suffix}") for suffix in self.subdomain_hosts
        )
        if not trusted:
            raise UnsafeNetworkTargetError("Provider request targeted an unapproved host.")
        return hostname, 443


def _resolve_public_addresses(hostname: str, port: int) -> tuple[_ResolvedAddress, ...]:
    try:
        answers = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise urllib.error.URLError(exc) from exc

    resolved: list[_ResolvedAddress] = []
    seen: set[_ResolvedAddress] = set()
    for family, socket_type, protocol, _canonical_name, raw_address in answers:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise UnsafeNetworkTargetError("Provider hostname resolved to an unsupported address.")
        address = cast(tuple[Any, ...], raw_address)
        try:
            ip = ipaddress.ip_address(str(address[0]).split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeNetworkTargetError(
                "Provider hostname resolved to an invalid address."
            ) from exc
        embedded: tuple[ipaddress.IPv4Address | None, ...] = ()
        if isinstance(ip, ipaddress.IPv6Address):
            teredo = ip.teredo or ()
            embedded = (ip.ipv4_mapped, ip.sixtofour, *teredo)
        if not ip.is_global or any(value is not None and not value.is_global for value in embedded):
            raise UnsafeNetworkTargetError(
                "Provider hostname resolved to a non-public network address."
            )
        if len(address) < 2 or address[1] != port:
            raise UnsafeNetworkTargetError("Provider hostname resolved to an unexpected port.")
        item = (int(family), int(socket_type), int(protocol), address)
        if item not in seen:
            seen.add(item)
            resolved.append(item)
    if not resolved:
        raise urllib.error.URLError("Provider hostname did not resolve to an address.")
    return tuple(resolved)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        *,
        resolved_addresses: tuple[_ResolvedAddress, ...],
        **kwargs: Any,
    ) -> None:
        super().__init__(host, **kwargs)
        self._resolved_addresses = resolved_addresses
        self._create_connection = self._open_pinned_socket

    def _open_pinned_socket(
        self,
        _address: tuple[str, int],
        timeout: Any,
        source_address: tuple[str, int] | None,
    ) -> socket.socket:
        last_error: OSError | None = None
        for family, socket_type, protocol, address in self._resolved_addresses:
            network_socket: socket.socket | None = None
            try:
                network_socket = socket.socket(family, socket_type, protocol)
                if timeout is None or isinstance(timeout, (int, float)):
                    network_socket.settimeout(timeout)
                if source_address:
                    network_socket.bind(source_address)
                network_socket.connect(address)
                return network_socket
            except OSError as exc:
                last_error = exc
                if network_socket is not None:
                    network_socket.close()
        if last_error is not None:
            raise last_error
        raise OSError("No validated provider address was available.")


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, policy: HTTPSRequestPolicy) -> None:
        super().__init__()
        self._policy = policy

    def https_open(self, request: urllib.request.Request) -> http.client.HTTPResponse:
        expected_host, expected_port = self._policy.validate(request.full_url)
        addresses = _resolve_public_addresses(expected_host, expected_port)

        def connection_factory(authority: str, **kwargs: Any) -> _PinnedHTTPSConnection:
            connection = _PinnedHTTPSConnection(
                authority,
                resolved_addresses=addresses,
                **kwargs,
            )
            actual_host = _canonical_hostname(connection.host)
            if actual_host != expected_host or connection.port != expected_port:
                raise UnsafeNetworkTargetError(
                    "Provider connection authority did not match its validated URL."
                )
            return connection

        return self.do_open(
            connection_factory, request, context=getattr(self, "_context", None)
        )


class _ValidatedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, policy: HTTPSRequestPolicy) -> None:
        super().__init__()
        self._policy = policy
        setattr(self, "max_redirections", policy.max_redirects)

    def redirect_request(
        self,
        request: urllib.request.Request,
        response: IO[bytes],
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        try:
            self._policy.validate(new_url)
        except UnsafeNetworkTargetError:
            response.close()
            raise
        return super().redirect_request(request, response, code, message, headers, new_url)


def open_validated_https(
    request: urllib.request.Request,
    *,
    timeout: float,
    policy: HTTPSRequestPolicy,
) -> BinaryIO:
    """Open one provider request without proxies after validating and pinning every hop."""

    policy.validate(request.full_url)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _ValidatedRedirectHandler(policy),
        _PinnedHTTPSHandler(policy),
    )
    return cast(BinaryIO, opener.open(request, timeout=timeout))
