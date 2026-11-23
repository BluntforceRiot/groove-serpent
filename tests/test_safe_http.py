from __future__ import annotations

import io
import socket
import unittest
import urllib.request
from unittest import mock

from groove_serpent.safe_http import (
    HTTPSRequestPolicy,
    UnsafeNetworkTargetError,
    _PinnedHTTPSConnection,
    _ValidatedRedirectHandler,
    _resolve_public_addresses,
    open_validated_https,
)


class SafeHTTPTests(unittest.TestCase):
    def test_redirect_policy_rejects_untrusted_destinations_before_following(self) -> None:
        policy = HTTPSRequestPolicy(
            ("coverartarchive.org", "archive.org"),
            subdomain_hosts=("archive.org",),
        )
        handler = _ValidatedRedirectHandler(policy)
        request = urllib.request.Request("https://coverartarchive.org/release/example")
        unsafe_destinations = (
            "http://archive.org/download/example",
            "https://127.0.0.1/internal",
            "https://169.254.169.254/latest/meta-data/",
            "https://archive.org.evil.example/download/example",
            "https://archive.org:8443/download/example",
            "https://user@archive.org/download/example",
        )

        for destination in unsafe_destinations:
            with self.subTest(destination=destination):
                response = io.BytesIO()
                with self.assertRaises(UnsafeNetworkTargetError):
                    handler.redirect_request(
                        request,
                        response,
                        302,
                        "Found",
                        {},
                        destination,
                    )
                self.assertTrue(response.closed)

    def test_redirect_policy_preserves_trusted_artwork_hosts(self) -> None:
        policy = HTTPSRequestPolicy(
            ("coverartarchive.org", "archive.org"),
            subdomain_hosts=("archive.org",),
        )
        handler = _ValidatedRedirectHandler(policy)
        request = urllib.request.Request("https://coverartarchive.org/release/example")

        redirected = handler.redirect_request(
            request,
            io.BytesIO(),
            302,
            "Found",
            {},
            "https://ia801.example.archive.org/download/example/front.jpg",
        )

        self.assertIsNotNone(redirected)
        self.assertEqual(
            redirected.full_url if redirected is not None else "",
            "https://ia801.example.archive.org/download/example/front.jpg",
        )
        self.assertEqual(handler.max_redirections, 5)

    def test_resolution_rejects_any_nonpublic_answer(self) -> None:
        public = (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", 443),
        )
        unsafe_answers = (
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("169.254.169.254", 443),
            ),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", 443, 0, 0)),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("::ffff:127.0.0.1", 443, 0, 0),
            ),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("2002:7f00:1::", 443, 0, 0),
            ),
        )

        with mock.patch(
            "groove_serpent.safe_http.socket.getaddrinfo",
            return_value=[public],
        ):
            self.assertEqual(
                _resolve_public_addresses("provider.example", 443)[0][3],
                ("93.184.216.34", 443),
            )

        for unsafe in unsafe_answers:
            with self.subTest(address=unsafe[4]), mock.patch(
                "groove_serpent.safe_http.socket.getaddrinfo",
                return_value=[public, unsafe],
            ):
                with self.assertRaisesRegex(UnsafeNetworkTargetError, "non-public"):
                    _resolve_public_addresses("provider.example", 443)

    def test_post_redirect_keeps_body_off_followed_request(self) -> None:
        policy = HTTPSRequestPolicy(("api.acoustid.org",))
        handler = _ValidatedRedirectHandler(policy)
        request = urllib.request.Request(
            "https://api.acoustid.org/v2/lookup",
            data=b"client=secret",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "GrooveSerpentTests/1.0",
            },
            method="POST",
        )

        redirected = handler.redirect_request(
            request,
            io.BytesIO(),
            302,
            "Found",
            {},
            "https://api.acoustid.org/v2/moved",
        )

        self.assertIsNotNone(redirected)
        self.assertEqual(redirected.get_method() if redirected is not None else "", "GET")
        self.assertIsNone(redirected.data if redirected is not None else b"")
        self.assertIsNone(redirected.get_header("Content-type") if redirected else "")
        self.assertEqual(
            redirected.get_header("User-agent") if redirected else "",
            "GrooveSerpentTests/1.0",
        )

    def test_connection_uses_only_pinned_socket_address(self) -> None:
        network_socket = mock.Mock()
        connection = _PinnedHTTPSConnection(
            "api.acoustid.org",
            resolved_addresses=(
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    ("93.184.216.34", 443),
                ),
            ),
        )

        with mock.patch(
            "groove_serpent.safe_http.socket.socket",
            return_value=network_socket,
        ), mock.patch("groove_serpent.safe_http.socket.getaddrinfo") as resolver:
            opened = connection._create_connection(
                ("api.acoustid.org", 443),
                4.0,
                None,
            )

        self.assertIs(opened, network_socket)
        network_socket.connect.assert_called_once_with(("93.184.216.34", 443))
        resolver.assert_not_called()

    def test_open_uses_proxy_free_bounded_handlers(self) -> None:
        policy = HTTPSRequestPolicy(("musicbrainz.org",), max_redirects=3)
        request = urllib.request.Request("https://musicbrainz.org/ws/2/release/")
        opener = mock.Mock()
        response = io.BytesIO(b"{}")
        opener.open.return_value = response

        with mock.patch(
            "groove_serpent.safe_http.urllib.request.build_opener",
            return_value=opener,
        ) as build_opener:
            opened = open_validated_https(request, timeout=7.0, policy=policy)

        self.assertIs(opened, response)
        handlers = build_opener.call_args.args
        self.assertIsInstance(handlers[0], urllib.request.ProxyHandler)
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], _ValidatedRedirectHandler)
        self.assertEqual(handlers[1].max_redirections, 3)
        opener.open.assert_called_once_with(request, timeout=7.0)


if __name__ == "__main__":
    unittest.main()
