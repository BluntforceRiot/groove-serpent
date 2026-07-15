from __future__ import annotations

import hashlib
import http.client
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from groove_serpent.album import (
    AlbumProject,
    AlbumSide,
    load_album_project,
    repin_album_sides,
    save_album_project,
)
from groove_serpent.album_review_server import AlbumReviewServer, serve_album_project
from groove_serpent.album_publication_durability import (
    AlbumPublicationVerificationReport,
    VerificationMismatch,
)
from groove_serpent.album_publication_executor import (
    AlbumPublicationExecutionReport,
    AlbumPublicationPreflightReport,
    _directory_identity,
    _journal,
)
from groove_serpent.album_publication_policy import ToolObservations
from groove_serpent.media import probe_audio, sha256_file
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.project_io import load_project, save_project


PUBLICATION_TOOLS = ToolObservations(
    groove_serpent_version="1.0.0",
    ffmpeg_version="ffmpeg publication-workbench-test",
    ffprobe_version="ffprobe publication-workbench-test",
    ffmpeg_executable_sha256="1" * 64,
    ffprobe_executable_sha256="2" * 64,
    ffmpeg_version_output_sha256="3" * 64,
    ffprobe_version_output_sha256="4" * 64,
)


def _ensure_resolvable_public_host(server: AlbumReviewServer) -> None:
    try:
        socket.getaddrinfo(server.session_auth.public_host, None)
    except OSError:
        server.session_auth._public_host = "127.0.0.1"  # type: ignore[attr-defined]


def _endpoint_connection(endpoint: object) -> http.client.HTTPConnection:
    port = getattr(endpoint, "port", None)
    if type(port) is not int:
        raise AssertionError("Expected a concrete loopback endpoint port.")
    return http.client.HTTPConnection("127.0.0.1", port, timeout=3)


def _endpoint_headers(
    endpoint: object,
    headers: dict[str, str] | None = None,
) -> dict[str, str]:
    netloc = getattr(endpoint, "netloc", "")
    if not isinstance(netloc, str) or not netloc:
        raise AssertionError("Expected a concrete loopback endpoint authority.")
    result = dict(headers or {})
    result.setdefault("Host", netloc)
    return result


class AlbumReviewServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        side_a = self._write_project("side-a", "First")
        side_b = self._write_project("side-b", "Second")
        self.album_path = self.directory / "album.groove-album.json"
        album = AlbumProject(
            metadata={"artist": "Example Artist", "album": "Example Album"},
            sides=[
                AlbumSide("A", 1, side_a.name),
                AlbumSide("B", 2, side_b.name),
            ],
        )
        repin_album_sides(album, self.album_path)
        save_album_project(album, self.album_path)

        # Give the browser a real changed side to approve and repin.
        project = load_project(side_a)
        project.metadata["genre"] = "Metal"
        save_project(project, side_a)

        self.server = AlbumReviewServer(("127.0.0.1", 0), self.album_path)
        _ensure_resolvable_public_host(self.server)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.port = self.server.server_address[1]
        self.authority = f"{self.server.session_auth.public_host}:{self.port}"
        self.base = self.server.session_auth.origin(port=self.port)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def _write_project(self, stem: str, title: str) -> Path:
        source = self.directory / f"{stem}.flac"
        payload = (f"immutable-{stem}".encode("utf-8")) * 8
        source.write_bytes(payload)
        stat = source.stat()
        project = Project(
            source=AudioSource(
                path=source.name,
                filename=source.name,
                size_bytes=stat.st_size,
                modified_ns=stat.st_mtime_ns,
                duration_seconds=10.0,
                sample_rate=1_000,
                channels=2,
                codec_name="flac",
                bits_per_raw_sample=16,
                sample_format="s16",
                sample_count=10_000,
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(
                music_start_seconds=0.0,
                music_end_seconds=10.0,
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=[
                Track(
                    number=1,
                    title=title,
                    start_sample=0,
                    end_sample=10_000,
                    start_seconds=0.0,
                    end_seconds=10.0,
                    artist="Example Artist",
                    album="Example Album",
                )
            ],
            metadata={"artist": "Example Artist", "album": "Example Album"},
        )
        project_path = self.directory / f"{stem}.groove.json"
        save_project(project, project_path)
        return project_path

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        headers: dict[str, str] | None = None,
        authenticate: bool = True,
        add_origin: bool = True,
    ) -> tuple[int, http.client.HTTPMessage, bytes]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = dict(headers or {})
        request_headers.setdefault("Host", self.authority)
        if authenticate:
            request_headers.setdefault(
                "Authorization", self.server.session_auth.authorization_header
            )
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")
            if add_origin:
                request_headers.setdefault("Origin", self.base)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        response_body = response.read()
        result = response.status, response.headers, response_body
        connection.close()
        return result

    def test_normalized_request_targets_are_rejected_before_bootstrap(self) -> None:
        bootstrap = self.server.session_auth.bootstrap_path
        for target in (
            f"/{bootstrap}",
            f"///{bootstrap.lstrip('/')}",
            "//album.js",
        ):
            with self.subTest(target=target):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", self.port, timeout=2
                )
                connection.putrequest("GET", target, skip_host=True)
                connection.putheader("Host", self.authority)
                connection.endheaders()
                response = connection.getresponse()
                body = response.read()
                self.assertEqual(response.status, 400, body)
                self.assertIsNone(response.headers.get("Set-Cookie"))
                self.assertTrue(response.will_close)
                self.assertNotIn(bootstrap.encode("ascii"), body)
                connection.close()

        status, headers, body = self.request(
            "GET", bootstrap, authenticate=False
        )
        self.assertEqual(status, 303, body)
        self.assertIsNotNone(headers.get("Set-Cookie"))

    def state(self) -> dict[str, object]:
        status, _headers, body = self.request("GET", "/api/album/state")
        self.assertEqual(status, 200, body)
        value = json.loads(body)
        self.assertIsInstance(value, dict)
        return value

    def test_album_session_auth_and_parent_child_isolation(self) -> None:
        self.assertRegex(
            self.server.session_auth.public_host,
            r"^(?:groove-serpent-[0-9a-f]{32}\.localhost|127\.0\.0\.1)$",
        )
        status, _headers, _body = self.request(
            "GET", "/api/album/state", authenticate=False
        )
        self.assertEqual(status, 401)

        status, headers, body = self.request(
            "GET",
            self.server.session_auth.bootstrap_path,
            authenticate=False,
        )
        self.assertEqual(status, 303, body)
        self.assertEqual(headers["Location"], "/")
        parent_set_cookie = headers["Set-Cookie"]
        self.assertIn("; Path=/; HttpOnly; SameSite=Strict", parent_set_cookie)
        parent_cookie = parent_set_cookie.split(";", 1)[0]

        status, replay_headers, body = self.request(
            "GET",
            self.server.session_auth.bootstrap_path,
            headers={"Cookie": parent_cookie},
            authenticate=False,
        )
        self.assertEqual(status, 303, body)
        self.assertEqual(replay_headers["Location"], "/")
        self.assertIsNone(replay_headers.get("Set-Cookie"))

        status, _headers, body = self.request(
            "GET",
            "/api/album/state",
            headers={"Cookie": parent_cookie},
            authenticate=False,
        )
        self.assertEqual(status, 200, body)
        parent_token = self.server.session_auth.authorization_header.removeprefix("Bearer ")
        self.assertNotIn(parent_token.encode("ascii"), body)
        state = json.loads(body)

        open_payload = self.open_side_payload(state)
        status, _headers, _body = self.request(
            "POST",
            "/api/album/open-side",
            payload=open_payload,
            headers={"Cookie": parent_cookie},
            authenticate=False,
            add_origin=False,
        )
        self.assertEqual(status, 403)
        status, _headers, _body = self.request(
            "POST",
            "/api/album/open-side",
            payload=open_payload,
            headers={
                "Cookie": parent_cookie,
                "Origin": f"http://127.0.0.1:{self.port}",
            },
            authenticate=False,
        )
        expected_status = (
            200 if self.server.session_auth.public_host == "127.0.0.1" else 403
        )
        self.assertEqual(status, expected_status)
        status, _headers, body = self.request(
            "POST",
            "/api/album/open-side",
            payload=open_payload,
            headers={"Cookie": parent_cookie},
            authenticate=False,
        )
        self.assertEqual(status, 200, body)

        status, _headers, body = self.request(
            "POST",
            "/api/album/repin",
            payload=self.repin_payload(state),
            add_origin=False,
        )
        self.assertEqual(status, 200, body)
        state = json.loads(body)

        for alias in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            with self.subTest(alias=alias):
                status, _headers, _body = self.request(
                    "GET",
                    "/api/album/state",
                    headers={"Host": alias},
                )
                self.assertEqual(status, 400)

        status, _headers, body = self.request(
            "POST",
            "/api/album/open-side",
            payload=self.open_side_payload(state),
            add_origin=False,
        )
        self.assertEqual(status, 200, body)
        opened = json.loads(body)
        endpoint = urlsplit(opened["url"])
        self.assertTrue(endpoint.path.startswith("/__groove_serpent_session__/"))
        self.assertFalse(endpoint.query)
        self.assertNotEqual(endpoint.path, self.server.session_auth.bootstrap_path)

        child = self.server._side_review_children["A"]
        self.assertEqual(endpoint.hostname, child.server.session_auth.public_host)
        self.assertNotEqual(
            self.server.session_auth.public_host,
            child.server.session_auth.public_host,
        )
        child_authorization = child.server.session_auth.authorization_header
        status, _headers, _body = self.request(
            "GET",
            "/api/album/state",
            headers={"Authorization": child_authorization},
            authenticate=False,
        )
        self.assertEqual(status, 401)

        child_connection = _endpoint_connection(endpoint)
        child_connection.request(
            "GET",
            "/api/project",
            headers=_endpoint_headers(
                endpoint,
                {"Authorization": self.server.session_auth.authorization_header},
            ),
        )
        child_response = child_connection.getresponse()
        child_response.read()
        self.assertEqual(child_response.status, 401)
        child_connection.close()

        child_connection = _endpoint_connection(endpoint)
        child_connection.request("GET", endpoint.path, headers=_endpoint_headers(endpoint))
        child_response = child_connection.getresponse()
        child_response.read()
        self.assertEqual(child_response.status, 303)
        child_set_cookie = child_response.headers["Set-Cookie"]
        child_cookie = child_set_cookie.split(";", 1)[0]
        self.assertNotEqual(child_cookie.split("=", 1)[0], parent_cookie.split("=", 1)[0])
        child_token = child_authorization.removeprefix("Bearer ")
        self.assertEqual(child_cookie.split("=", 1)[1], child_token)
        self.assertNotEqual(endpoint.path.rsplit("/", 1)[-1], child_token)
        child_connection.close()

        child_connection = _endpoint_connection(endpoint)
        child_connection.request("GET", endpoint.path, headers=_endpoint_headers(endpoint))
        child_response = child_connection.getresponse()
        child_response.read()
        self.assertEqual(child_response.status, 401)
        child_connection.close()

        child_connection = _endpoint_connection(endpoint)
        child_connection.request(
            "GET",
            "/api/project",
            headers=_endpoint_headers(endpoint, {"Cookie": parent_cookie}),
        )
        child_response = child_connection.getresponse()
        child_response.read()
        self.assertEqual(child_response.status, 401)
        child_connection.close()

        status, _headers, _body = self.request(
            "GET",
            "/api/album/state",
            headers={"Cookie": child_cookie},
            authenticate=False,
        )
        self.assertEqual(status, 401)

        child_connection = _endpoint_connection(endpoint)
        combined_cookies = f"{parent_cookie}; {child_cookie}"
        child_connection.request(
            "GET",
            "/api/project",
            headers=_endpoint_headers(endpoint, {"Cookie": combined_cookies}),
        )
        child_response = child_connection.getresponse()
        project_body = child_response.read()
        self.assertEqual(child_response.status, 200, project_body)
        child_connection.close()

        status, _headers, body = self.request(
            "GET",
            "/api/album/state",
            headers={"Cookie": combined_cookies},
            authenticate=False,
        )
        self.assertEqual(status, 200, body)

    @staticmethod
    def repin_payload(state: dict[str, object], label: str = "A") -> dict[str, object]:
        sides = state["sides"]
        assert isinstance(sides, list)
        side = next(item for item in sides if item["label"] == label)
        return {
            "expected_album_sha256": state["album_project_sha256"],
            "expected_album_revision": state["album_revision"],
            "side_label": label,
            "expected_current_identity": side["current_identity"],
            "reviewed": True,
        }

    @staticmethod
    def open_side_payload(state: dict[str, object], label: str = "A") -> dict[str, object]:
        sides = state["sides"]
        assert isinstance(sides, list)
        side = next(item for item in sides if item["label"] == label)
        return {
            "expected_album_sha256": state["album_project_sha256"],
            "expected_album_revision": state["album_revision"],
            "side_label": label,
            "expected_current_identity": side["current_identity"],
        }

    @staticmethod
    def mutation_preconditions(state: dict[str, object]) -> dict[str, object]:
        sides = state["sides"]
        assert isinstance(sides, list)
        return {
            "expected_album_sha256": state["album_project_sha256"],
            "expected_album_revision": state["album_revision"],
            "expected_sides": [
                {
                    "side_label": side["label"],
                    "current_identity": side["current_identity"],
                }
                for side in sides
            ],
        }

    @classmethod
    def publication_payload(
        cls,
        state: dict[str, object],
        *,
        filename: str = "album-test.publication-plan.json",
    ) -> dict[str, object]:
        return {
            **cls.mutation_preconditions(state),
            "action": "create-reviewed-publication-plan",
            "reviewed": True,
            "plan_filename": filename,
            "selected_profiles": ["archival-source"],
            "restoration_mode": "none",
            "flac_compression": 8,
            "aac_bitrate_kbps": 256,
        }

    def _replace_sources_with_real_flac(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None or shutil.which("ffprobe") is None:
            self.skipTest("FFmpeg and ffprobe are required for publication execution.")
        for index, stem in enumerate(("side-a", "side-b"), start=1):
            source = self.directory / f"{stem}.flac"
            subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={440 + index * 110}:sample_rate=48000:duration=0.2",
                    "-ac",
                    "2",
                    "-c:a",
                    "flac",
                    "-sample_fmt",
                    "s16",
                    str(source),
                ],
                check=True,
            )
            project_path = self.directory / f"{stem}.groove.json"
            previous = load_project(project_path)
            audio = probe_audio(source, stored_path=source.name)
            analysis = AnalysisSummary(
                music_start_seconds=0.0,
                music_end_seconds=audio.duration_seconds,
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            )
            tracks = [
                Track(
                    number=1,
                    title=f"Track {index}",
                    start_sample=0,
                    end_sample=audio.sample_count,
                    start_seconds=0.0,
                    end_seconds=audio.duration_seconds,
                    artist="Example Artist",
                    album="Example Album",
                )
            ]
            save_project(
                Project(
                    source=audio,
                    settings=previous.settings,
                    analysis=analysis,
                    tracks=tracks,
                    metadata=dict(previous.metadata),
                    revision=previous.revision,
                ),
                project_path,
            )
        album = load_album_project(self.album_path)
        repin_album_sides(album, self.album_path)
        save_album_project(album, self.album_path, overwrite=True)

    def test_state_static_assets_ping_and_security_headers(self) -> None:
        state = self.state()
        self.assertEqual(state["schema"], "groove-serpent.album-workbench/4")
        self.assertEqual(state["album_project_sha256"], sha256_file(self.album_path))
        self.assertEqual(state["album_revision"], load_album_project(self.album_path).revision)
        self.assertEqual(state["total_sides"], 2)

        for path, content_type in (
            ("/", "text/html"),
            ("/album.js", "text/javascript"),
            ("/album.css", "text/css"),
            ("/styles.css", "text/css"),
        ):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertIn(content_type, headers["Content-Type"])
                self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")

        status, _headers, body = self.request("GET", "/api/ping")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

        _status, _headers, html = self.request("GET", "/")
        _status, _headers, script = self.request("GET", "/album.js")
        script_text = script.decode("utf-8")
        html_text = html.decode("utf-8")
        self.assertNotIn("Â", script_text)
        self.assertNotIn("â", script_text)
        self.assertNotIn("Â", html_text)
        self.assertNotIn("â", html_text)
        self.assertIn(b"Open any exact side", html)
        self.assertIn(b'"/api/album/open-side"', script)
        self.assertIn(b"expected_current_identity", script)
        self.assertIn(b"expected_album_revision", script)
        self.assertIn(b'"/api/album/add-side"', script)
        self.assertIn(b'"/api/album/remove-side"', script)
        self.assertIn(b'"/api/album/reorder-sides"', script)
        self.assertIn(b'"/api/album/update-details"', script)
        self.assertIn(b'"/api/album/publication/create-plan"', script)
        self.assertIn(b'"/api/album/publication/preflight"', script)
        self.assertIn(b'"/api/album/publication/execute"', script)
        self.assertIn(b'"/api/album/publication/verify"', script)
        self.assertIn(b'"/api/album/publication/replay"', script)
        self.assertIn(b'"/api/album/publication/recover"', script)
        self.assertIn(b"\\u00B7", script)
        self.assertIn(b"Exact name:", html)
        self.assertIn(b"side-open-link", script)
        self.assertIn(b"Pair sides and edit album details", html)
        self.assertIn(b"DELIBERATE REMOVAL", html)
        self.assertIn(b"RESTART-SAFE PUBLICATION", html)
        self.assertIn(b"one byte-identical full-capture object", html)
        self.assertNotIn(b"materialized more than once", html)
        archival_profile = next(
            profile
            for profile in state["publication"]["choices"]["profiles"]
            if profile["id"] == "archival-source"
        )
        self.assertIn(
            "per unique exact source identity",
            archival_profile["description"],
        )

    def test_reviewed_plan_creation_preflight_and_restart_reopen(self) -> None:
        initial = self.state()
        status, _headers, body = self.request(
            "POST",
            "/api/album/repin",
            payload=self.repin_payload(initial),
        )
        self.assertEqual(status, 200, body)
        ready = json.loads(body)
        self.assertTrue(ready["publication"]["readiness"]["can_create_plan"])
        album_before_plan = self.album_path.read_bytes()
        plan_path = self.directory / "album-test.publication-plan.json"

        with (
            patch(
                "groove_serpent.album_publication_builder.observe_publication_tools",
                return_value=PUBLICATION_TOOLS,
            ),
            patch(
                "groove_serpent.album_publication_executor.observe_publication_tools",
                return_value=PUBLICATION_TOOLS,
            ),
        ):
            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/create-plan",
                payload=self.publication_payload(ready),
            )
            self.assertEqual(status, 201, body)
            created = json.loads(body)
            self.assertTrue(created["ok"])
            digest = created["created_plan"]["plan_sha256"]
            self.assertTrue(plan_path.is_file())
            self.assertEqual(self.album_path.read_bytes(), album_before_plan)
            current = created["state"]["publication"]["catalog"]["entries"]
            self.assertEqual(
                [(entry["filename"], entry["status"]) for entry in current],
                [(plan_path.name, "current")],
            )

            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/preflight",
                payload={
                    **self.mutation_preconditions(created["state"]),
                    "action": "preflight-current-publication-plan",
                    "plan_sha256": digest,
                },
            )
            self.assertEqual(status, 200, body)
            preflight = json.loads(body)
            self.assertEqual(preflight["preflight"]["plan_sha256"], digest)
            self.assertEqual(preflight["preflight"]["filename"], plan_path.name)

            reopened = AlbumReviewServer(("127.0.0.1", 0), self.album_path)
            reopened_thread = threading.Thread(
                target=reopened.serve_forever,
                kwargs={"poll_interval": 0.01},
                daemon=True,
            )
            reopened_thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    reopened.server_address[1],
                    timeout=3,
                )
                connection.request(
                    "GET",
                    "/api/album/state",
                    headers={
                        "Authorization": reopened.session_auth.authorization_header,
                        "Host": (
                            f"{reopened.session_auth.public_host}:"
                            f"{reopened.server_address[1]}"
                        ),
                    },
                )
                response = connection.getresponse()
                reopened_body = response.read()
                connection.close()
                self.assertEqual(response.status, 200, reopened_body)
                reopened_state = json.loads(reopened_body)
                reopened_entries = reopened_state["publication"]["catalog"]["entries"]
                self.assertEqual(reopened_entries[0]["plan_sha256"], digest)
                self.assertEqual(reopened_entries[0]["status"], "current")
            finally:
                reopened.shutdown()
                reopened.server_close()
                reopened_thread.join(timeout=2)

        self.assertEqual(self.album_path.read_bytes(), album_before_plan)

    def test_plan_creation_requires_review_and_never_replaces_a_name(self) -> None:
        initial = self.state()
        status, _headers, body = self.request(
            "POST",
            "/api/album/repin",
            payload=self.repin_payload(initial),
        )
        self.assertEqual(status, 200, body)
        ready = json.loads(body)
        plan_path = self.directory / "blocked.publication-plan.json"
        plan_path.write_bytes(b"owner bytes")
        payload = self.publication_payload(ready, filename=plan_path.name)
        payload["reviewed"] = False
        status, _headers, body = self.request(
            "POST",
            "/api/album/publication/create-plan",
            payload=payload,
        )
        self.assertEqual(status, 400, body)
        self.assertEqual(plan_path.read_bytes(), b"owner bytes")

        payload["reviewed"] = True
        with patch(
            "groove_serpent.album_publication_builder.observe_publication_tools",
            return_value=PUBLICATION_TOOLS,
        ):
            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/create-plan",
                payload=payload,
            )
        self.assertEqual(status, 400, body)
        self.assertEqual(plan_path.read_bytes(), b"owner bytes")

    def test_execute_verify_replay_and_restart_rediscovery(self) -> None:
        self._replace_sources_with_real_flac()
        ready = self.state()
        plan_filename = "execution.publication-plan.json"
        destination = "published-album"
        replay_destination = "published-album-replay"

        with (
            patch(
                "groove_serpent.album_publication_builder.observe_publication_tools",
                return_value=PUBLICATION_TOOLS,
            ),
            patch(
                "groove_serpent.album_publication_executor.observe_publication_tools",
                return_value=PUBLICATION_TOOLS,
            ),
        ):
            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/create-plan",
                payload=self.publication_payload(ready, filename=plan_filename),
            )
            self.assertEqual(status, 201, body)
            created = json.loads(body)
            plan = next(
                entry
                for entry in created["state"]["publication"]["catalog"]["entries"]
                if entry["status"] == "current"
            )
            execute_payload = {
                **self.mutation_preconditions(created["state"]),
                "action": "execute-current-publication-plan",
                "owner_confirmed": True,
                "confirmation": f"PUBLISH {destination}",
                "plan_sha256": plan["plan_sha256"],
                "plan_file_sha256": plan["file_sha256"],
                "destination_name": destination,
            }
            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/execute",
                payload=execute_payload,
            )
            self.assertEqual(status, 201, body)
            executed = json.loads(body)
            self.assertTrue(executed["ok"])
            self.assertEqual(executed["completion"], "verified")
            self.assertTrue(executed["verification"]["ok"])
            self.assertTrue(executed["restart_rediscovered"])
            self.assertTrue(executed["progress"])
            self.assertTrue((self.directory / destination).is_dir())
            receipt = next(
                entry
                for entry in executed["state"]["publication"]["operations"]["publications"]
                if entry["directory_name"] == destination
            )
            self.assertEqual(receipt["status"], "current")

            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/verify",
                payload={
                    **self.mutation_preconditions(executed["state"]),
                    "action": "verify-discovered-publication",
                    "directory_name": destination,
                    "manifest_sha256": receipt["manifest_sha256"],
                    "journal_sha256": receipt["journal_sha256"],
                    "plan_sha256": receipt["plan_sha256"],
                },
            )
            self.assertEqual(status, 200, body)
            verified = json.loads(body)
            self.assertTrue(verified["ok"])
            self.assertTrue(verified["read_only"])

            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/replay",
                payload={
                    **self.mutation_preconditions(verified["state"]),
                    "action": "replay-current-publication",
                    "owner_confirmed": True,
                    "confirmation": (f"REPLAY {destination} TO {replay_destination}"),
                    "plan_sha256": plan["plan_sha256"],
                    "plan_file_sha256": plan["file_sha256"],
                    "source_directory_name": destination,
                    "source_manifest_sha256": receipt["manifest_sha256"],
                    "source_journal_sha256": receipt["journal_sha256"],
                    "destination_name": replay_destination,
                },
            )
            self.assertEqual(status, 201, body)
            replayed = json.loads(body)
            self.assertTrue(replayed["ok"])
            self.assertEqual(replayed["completion"], "verified-match")
            self.assertTrue(replayed["replay"]["ok"])
            self.assertTrue((self.directory / replay_destination).is_dir())

            reopened = AlbumReviewServer(("127.0.0.1", 0), self.album_path)
            reopened_thread = threading.Thread(
                target=reopened.serve_forever,
                kwargs={"poll_interval": 0.01},
                daemon=True,
            )
            reopened_thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    reopened.server_address[1],
                    timeout=30,
                )
                connection.request(
                    "GET",
                    "/api/album/state",
                    headers={
                        "Authorization": reopened.session_auth.authorization_header,
                        "Host": (
                            f"{reopened.session_auth.public_host}:"
                            f"{reopened.server_address[1]}"
                        ),
                    },
                )
                response = connection.getresponse()
                reopened_body = response.read()
                connection.close()
                self.assertEqual(response.status, 200, reopened_body)
                reopened_state = json.loads(reopened_body)
                reopened_names = {
                    item["directory_name"]
                    for item in reopened_state["publication"]["operations"]["publications"]
                    if item["status"] == "current"
                }
                self.assertEqual(
                    reopened_names,
                    {destination, replay_destination},
                )
            finally:
                reopened.shutdown()
                reopened.server_close()
                reopened_thread.join(timeout=2)

    def test_orphan_quarantine_is_preferred_and_remove_is_stronger(self) -> None:
        initial = self.state()
        status, _headers, body = self.request(
            "POST",
            "/api/album/repin",
            payload=self.repin_payload(initial),
        )
        self.assertEqual(status, 200, body)
        ready = json.loads(body)

        with (
            patch(
                "groove_serpent.album_publication_builder.observe_publication_tools",
                return_value=PUBLICATION_TOOLS,
            ),
            patch(
                "groove_serpent.album_publication_executor.observe_publication_tools",
                return_value=PUBLICATION_TOOLS,
            ),
        ):
            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/create-plan",
                payload=self.publication_payload(
                    ready,
                    filename="recovery.publication-plan.json",
                ),
            )
            self.assertEqual(status, 201, body)
            created = json.loads(body)
            plan = next(
                entry
                for entry in created["state"]["publication"]["catalog"]["entries"]
                if entry["status"] == "current"
            )
            operation_id = "1" * 32
            stage = self.directory / (f".groove-serpent-album-publication-{operation_id}.partial")
            stage.mkdir()
            identity = _directory_identity(stage, label="Synthetic server orphan")
            _journal(
                stage,
                "staging",
                plan["plan_sha256"],
                operation_id=operation_id,
                intended_output_name="unfinished-publication",
                stage_identity=identity,
            )
            state = self.state()
            orphan = state["publication"]["operations"]["orphans"][0]
            self.assertTrue(orphan["actionable"])

            weak_remove = {
                **self.mutation_preconditions(state),
                "action": "recover-owned-publication-orphan",
                "owner_confirmed": True,
                "confirmation": f"REMOVE {orphan['directory_name']}",
                "recovery_action": "remove",
                "orphan_directory_name": orphan["directory_name"],
                "orphan_kind": orphan["kind"],
                "plan_sha256": orphan["plan_sha256"],
                "journal_sha256": orphan["journal_sha256"],
                "directory_identity": orphan["directory_identity"],
            }
            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/recover",
                payload=weak_remove,
            )
            self.assertEqual(status, 400, body)
            self.assertTrue(stage.is_dir())

            quarantine = {
                **weak_remove,
                "confirmation": f"QUARANTINE {orphan['directory_name']}",
                "recovery_action": "quarantine",
            }
            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/recover",
                payload=quarantine,
            )
            self.assertEqual(status, 200, body)
            quarantined = json.loads(body)
            self.assertTrue(quarantined["ok"])
            self.assertFalse(stage.exists())
            quarantine_orphan = quarantined["state"]["publication"]["operations"]["orphans"][0]
            self.assertEqual(quarantine_orphan["kind"], "quarantine")

            strong_remove = {
                **self.mutation_preconditions(quarantined["state"]),
                "action": "recover-owned-publication-orphan",
                "owner_confirmed": True,
                "confirmation": (
                    "REMOVE OWNED ORPHAN "
                    f"{quarantine_orphan['directory_name']} "
                    f"{quarantine_orphan['journal_sha256']}"
                ),
                "recovery_action": "remove",
                "orphan_directory_name": quarantine_orphan["directory_name"],
                "orphan_kind": quarantine_orphan["kind"],
                "plan_sha256": quarantine_orphan["plan_sha256"],
                "journal_sha256": quarantine_orphan["journal_sha256"],
                "directory_identity": quarantine_orphan["directory_identity"],
            }
            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/recover",
                payload=strong_remove,
            )
            self.assertEqual(status, 200, body)
            removed = json.loads(body)
            self.assertTrue(removed["recovery"]["removed"])
            self.assertEqual(
                removed["state"]["publication"]["operations"]["orphans"],
                [],
            )

    def test_execute_and_recovery_reject_escape_missing_authority_and_race(self) -> None:
        initial = self.state()
        status, _headers, body = self.request(
            "POST",
            "/api/album/repin",
            payload=self.repin_payload(initial),
        )
        self.assertEqual(status, 200, body)
        ready = json.loads(body)
        with (
            patch(
                "groove_serpent.album_publication_builder.observe_publication_tools",
                return_value=PUBLICATION_TOOLS,
            ),
            patch(
                "groove_serpent.album_publication_executor.observe_publication_tools",
                return_value=PUBLICATION_TOOLS,
            ),
        ):
            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/create-plan",
                payload=self.publication_payload(
                    ready,
                    filename="adversarial.publication-plan.json",
                ),
            )
            self.assertEqual(status, 201, body)
            created = json.loads(body)
            plan = created["state"]["publication"]["catalog"]["entries"][0]
            base = {
                **self.mutation_preconditions(created["state"]),
                "action": "execute-current-publication-plan",
                "owner_confirmed": False,
                "confirmation": "PUBLISH safe-output",
                "plan_sha256": plan["plan_sha256"],
                "plan_file_sha256": plan["file_sha256"],
                "destination_name": "safe-output",
            }
            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/execute",
                payload=base,
            )
            self.assertEqual(status, 400, body)
            self.assertFalse((self.directory / "safe-output").exists())
            escape = {
                **base,
                "owner_confirmed": True,
                "destination_name": "../outside",
                "confirmation": "PUBLISH ../outside",
            }
            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/execute",
                payload=escape,
            )
            self.assertEqual(status, 400, body)

            operation_id = "2" * 32
            stage = self.directory / (f".groove-serpent-album-publication-{operation_id}.partial")
            stage.mkdir()
            identity = _directory_identity(stage, label="Synthetic race orphan")
            _journal(
                stage,
                "staging",
                plan["plan_sha256"],
                operation_id=operation_id,
                intended_output_name="race-output",
                stage_identity=identity,
            )
            state = self.state()
            orphan = state["publication"]["operations"]["orphans"][0]
            journal = stage / "groove-serpent-publication-journal.json"
            journal.write_bytes(journal.read_bytes() + b"\n")
            recovery = {
                **self.mutation_preconditions(state),
                "action": "recover-owned-publication-orphan",
                "owner_confirmed": True,
                "confirmation": f"QUARANTINE {orphan['directory_name']}",
                "recovery_action": "quarantine",
                "orphan_directory_name": orphan["directory_name"],
                "orphan_kind": orphan["kind"],
                "plan_sha256": orphan["plan_sha256"],
                "journal_sha256": orphan["journal_sha256"],
                "directory_identity": orphan["directory_identity"],
            }
            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/recover",
                payload=recovery,
            )
            self.assertEqual(status, 409, body)
            self.assertTrue(stage.is_dir())

    def test_execution_does_not_claim_completion_when_verification_fails(self) -> None:
        initial = self.state()
        status, _headers, body = self.request(
            "POST",
            "/api/album/repin",
            payload=self.repin_payload(initial),
        )
        self.assertEqual(status, 200, body)
        ready = json.loads(body)
        with (
            patch(
                "groove_serpent.album_publication_builder.observe_publication_tools",
                return_value=PUBLICATION_TOOLS,
            ),
            patch(
                "groove_serpent.album_publication_executor.observe_publication_tools",
                return_value=PUBLICATION_TOOLS,
            ),
        ):
            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/create-plan",
                payload=self.publication_payload(
                    ready,
                    filename="failed-verification.publication-plan.json",
                ),
            )
            self.assertEqual(status, 201, body)
            created = json.loads(body)
            plan = created["state"]["publication"]["catalog"]["entries"][0]
            destination = "unverified-output"
            preflight = AlbumPublicationPreflightReport(
                plan_sha256=plan["plan_sha256"],
                album_sha256=created["state"]["album_project_sha256"],
                selected_profiles=("archival-source",),
                side_count=2,
            )
            execution = AlbumPublicationExecutionReport(
                output_directory=str(self.directory / destination),
                manifest_path=str(
                    self.directory / destination / "groove-serpent-album-publication.json"
                ),
                plan_sha256=plan["plan_sha256"],
                artifacts=(),
            )
            verification = AlbumPublicationVerificationReport(
                publication_directory=str(self.directory / destination),
                ok=False,
                manifest_sha256=None,
                journal_sha256=None,
                artifact_count=0,
                mismatches=(
                    VerificationMismatch(
                        "verification_failed",
                        None,
                        "strict verified publication",
                        None,
                        "Synthetic post-commit verification failure.",
                    ),
                ),
            )
            with (
                patch(
                    "groove_serpent.album_review_server.preflight_album_publication_plan",
                    return_value=preflight,
                ),
                patch(
                    "groove_serpent.album_review_server.execute_album_publication_plan",
                    return_value=execution,
                ),
                patch(
                    "groove_serpent.album_review_server.verify_album_publication",
                    return_value=verification,
                ),
            ):
                status, _headers, body = self.request(
                    "POST",
                    "/api/album/publication/execute",
                    payload={
                        **self.mutation_preconditions(created["state"]),
                        "action": "execute-current-publication-plan",
                        "owner_confirmed": True,
                        "confirmation": f"PUBLISH {destination}",
                        "plan_sha256": plan["plan_sha256"],
                        "plan_file_sha256": plan["file_sha256"],
                        "destination_name": destination,
                    },
                )
            self.assertEqual(status, 200, body)
            result = json.loads(body)
            self.assertFalse(result["ok"])
            self.assertEqual(result["completion"], "verification-failed")
            self.assertFalse(result["restart_rediscovered"])

    def test_plan_file_race_after_preflight_prevents_execution(self) -> None:
        initial = self.state()
        status, _headers, body = self.request(
            "POST",
            "/api/album/repin",
            payload=self.repin_payload(initial),
        )
        self.assertEqual(status, 200, body)
        ready = json.loads(body)
        plan_filename = "race-after-preflight.publication-plan.json"
        with (
            patch(
                "groove_serpent.album_publication_builder.observe_publication_tools",
                return_value=PUBLICATION_TOOLS,
            ),
            patch(
                "groove_serpent.album_publication_executor.observe_publication_tools",
                return_value=PUBLICATION_TOOLS,
            ),
        ):
            status, _headers, body = self.request(
                "POST",
                "/api/album/publication/create-plan",
                payload=self.publication_payload(ready, filename=plan_filename),
            )
            self.assertEqual(status, 201, body)
            created = json.loads(body)
            plan = created["state"]["publication"]["catalog"]["entries"][0]
            plan_path = self.directory / plan_filename

            def race(_path: Path) -> AlbumPublicationPreflightReport:
                plan_path.write_bytes(plan_path.read_bytes() + b"\n")
                return AlbumPublicationPreflightReport(
                    plan_sha256=plan["plan_sha256"],
                    album_sha256=created["state"]["album_project_sha256"],
                    selected_profiles=("archival-source",),
                    side_count=2,
                )

            with (
                patch(
                    "groove_serpent.album_review_server.preflight_album_publication_plan",
                    side_effect=race,
                ),
                patch(
                    "groove_serpent.album_review_server.execute_album_publication_plan"
                ) as execute,
            ):
                status, _headers, body = self.request(
                    "POST",
                    "/api/album/publication/execute",
                    payload={
                        **self.mutation_preconditions(created["state"]),
                        "action": "execute-current-publication-plan",
                        "owner_confirmed": True,
                        "confirmation": "PUBLISH race-output",
                        "plan_sha256": plan["plan_sha256"],
                        "plan_file_sha256": plan["file_sha256"],
                        "destination_name": "race-output",
                    },
                )
            self.assertEqual(status, 409, body)
            execute.assert_not_called()
            self.assertFalse((self.directory / "race-output").exists())

    def test_open_side_returns_exact_child_and_reuses_it(self) -> None:
        state = self.state()
        payload = self.open_side_payload(state)
        status, _headers, body = self.request("POST", "/api/album/open-side", payload=payload)
        self.assertEqual(status, 200, body)
        opened = json.loads(body)
        self.assertEqual(
            set(opened),
            {"ok", "url", "side_label", "current_identity", "reused"},
        )
        self.assertTrue(opened["ok"])
        self.assertEqual(opened["side_label"], "A")
        self.assertEqual(opened["current_identity"], payload["expected_current_identity"])
        self.assertFalse(opened["reused"])
        endpoint = urlsplit(opened["url"])
        self.assertEqual(endpoint.scheme, "http")
        child = self.server._side_review_children["A"]
        self.assertEqual(endpoint.hostname, child.server.session_auth.public_host)
        self.assertNotEqual(endpoint.hostname, self.server.session_auth.public_host)
        self.assertIsNotNone(endpoint.port)
        self.assertTrue(endpoint.path.startswith("/__groove_serpent_session__/"))
        self.assertFalse(endpoint.query)
        connection = _endpoint_connection(endpoint)
        connection.request("GET", endpoint.path, headers=_endpoint_headers(endpoint))
        response = connection.getresponse()
        bootstrap_body = response.read()
        self.assertEqual(response.status, 303, bootstrap_body)
        self.assertEqual(response.headers["Location"], "/")
        cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        connection.close()

        connection = _endpoint_connection(endpoint)
        connection.request(
            "GET",
            "/api/project",
            headers=_endpoint_headers(endpoint, {"Cookie": cookie}),
        )
        response = connection.getresponse()
        project_body = response.read()
        connection.close()
        self.assertEqual(response.status, 200, project_body)
        project = json.loads(project_body)
        self.assertEqual(project["tracks"][0]["title"], "First")

        status, _headers, body = self.request("POST", "/api/album/open-side", payload=payload)
        self.assertEqual(status, 200, body)
        reused = json.loads(body)
        self.assertTrue(reused["reused"])
        self.assertNotEqual(reused["url"], opened["url"])
        reused_endpoint = urlsplit(reused["url"])
        self.assertEqual(reused_endpoint.port, endpoint.port)
        connection = _endpoint_connection(reused_endpoint)
        connection.request(
            "GET",
            reused_endpoint.path,
            headers=_endpoint_headers(reused_endpoint),
        )
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 303)
        self.assertEqual(
            response.headers["Set-Cookie"].split(";", 1)[0],
            cookie,
        )
        connection.close()

    def test_open_side_compares_every_identity_field_and_exact_schema(self) -> None:
        state = self.state()
        original = self.open_side_payload(state)
        for key in (
            "project_revision",
            "project_sha256",
            "editable_state_sha256",
            "source_sha256",
            "project_speed_state_sha256",
        ):
            payload = json.loads(json.dumps(original))
            identity = payload["expected_current_identity"]
            identity[key] = identity[key] + 1 if key == "project_revision" else "0" * 64
            with self.subTest(key=key):
                status, _headers, body = self.request(
                    "POST", "/api/album/open-side", payload=payload
                )
                self.assertEqual(status, 409, body)

        for payload in (
            {**original, "reviewed": True},
            {key: value for key, value in original.items() if key != "expected_album_sha256"},
        ):
            with self.subTest(payload=payload):
                status, _headers, body = self.request(
                    "POST", "/api/album/open-side", payload=payload
                )
                self.assertEqual(status, 400, body)

        status, _headers, body = self.request(
            "POST", "/api/album/open-side?project=elsewhere", payload=original
        )
        self.assertEqual(status, 400, body)

    def test_open_side_replaces_drifted_child_and_closes_its_snapshot(self) -> None:
        state = self.state()
        payload = self.open_side_payload(state)
        status, _headers, body = self.request("POST", "/api/album/open-side", payload=payload)
        self.assertEqual(status, 200, body)
        first = json.loads(body)
        old_child = self.server._side_review_children["A"]
        old_snapshot = old_child.server.source_snapshot.path
        self.assertTrue(old_snapshot.is_file())

        project_path = self.directory / "side-a.groove.json"
        project = load_project(project_path)
        project.metadata["year"] = "2026"
        save_project(project, project_path)
        updated_state = self.state()
        updated_payload = self.open_side_payload(updated_state)
        status, _headers, body = self.request(
            "POST", "/api/album/open-side", payload=updated_payload
        )
        self.assertEqual(status, 200, body)
        replacement = json.loads(body)
        self.assertFalse(replacement["reused"])
        self.assertNotEqual(replacement["url"], first["url"])
        self.assertFalse(old_child.thread.is_alive())
        self.assertFalse(old_snapshot.exists())

    def test_simultaneous_exact_open_does_not_orphan_a_child(self) -> None:
        state = self.state()
        sides = state["sides"]
        assert isinstance(sides, list)
        side = next(item for item in sides if item["label"] == "A")
        identity = side["current_identity"]
        assert isinstance(identity, dict)
        project_path = self.directory / "side-a.groove.json"
        barrier = threading.Barrier(2)
        original_wait = self.server._wait_for_side_review

        def synchronized_wait(child: object) -> None:
            original_wait(child)  # type: ignore[arg-type]
            barrier.wait(timeout=5)

        with patch.object(
            self.server,
            "_wait_for_side_review",
            side_effect=synchronized_wait,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        self.server.open_side_review,
                        "A",
                        project_path,
                        identity,
                    )
                    for _index in range(2)
                ]
                results = [future.result(timeout=10) for future in futures]

        self.assertEqual({child.url for child, _reused in results}, {results[0][0].url})
        self.assertEqual(sorted(reused for _child, reused in results), [False, True])
        live_named_threads = [
            thread
            for thread in threading.enumerate()
            if thread.name == "groove-serpent-side-A-review"
        ]
        self.assertEqual(len(live_named_threads), 1)
        self.assertEqual(len(self.server._side_review_children), 1)

    def test_parent_close_stops_children_and_removes_snapshots(self) -> None:
        state = self.state()
        status, _headers, body = self.request(
            "POST",
            "/api/album/open-side",
            payload=self.open_side_payload(state),
        )
        self.assertEqual(status, 200, body)
        child = self.server._side_review_children["A"]
        snapshot = child.server.source_snapshot.path
        self.assertTrue(snapshot.is_file())

        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(child.thread.is_alive())
        self.assertEqual(child.server.socket.fileno(), -1)
        self.assertFalse(snapshot.exists())
        self.assertEqual(self.server._side_review_children, {})

    def test_repin_requires_exact_reviewed_identity_and_returns_new_state(self) -> None:
        state = self.state()
        payload = self.repin_payload(state)
        old_digest = state["album_project_sha256"]
        status, _headers, body = self.request("POST", "/api/album/repin", payload=payload)
        self.assertEqual(status, 200, body)
        updated = json.loads(body)
        self.assertNotEqual(updated["album_project_sha256"], old_digest)
        side = next(item for item in updated["sides"] if item["label"] == "A")
        current = side["current_identity"]
        pin = side["pin"]
        for key in (
            "project_revision",
            "project_sha256",
            "editable_state_sha256",
            "source_sha256",
            "project_speed_state_sha256",
        ):
            self.assertEqual(pin[key], current[key])
        saved = load_album_project(self.album_path)
        self.assertEqual(saved.sides[0].pin.project_sha256, current["project_sha256"])
        self.assertEqual(
            saved.sides[1].pin.project_sha256,
            state["sides"][1]["pin"]["project_sha256"],
        )

        status, _headers, body = self.request("POST", "/api/album/repin", payload=payload)
        self.assertEqual(status, 409, body)

    def test_every_expected_side_identity_field_is_compared(self) -> None:
        state = self.state()
        original = self.repin_payload(state)
        for key in (
            "project_revision",
            "project_sha256",
            "editable_state_sha256",
            "source_sha256",
            "project_speed_state_sha256",
        ):
            payload = json.loads(json.dumps(original))
            identity = payload["expected_current_identity"]
            identity[key] = identity[key] + 1 if key == "project_revision" else "0" * 64
            with self.subTest(key=key):
                status, _headers, body = self.request("POST", "/api/album/repin", payload=payload)
                self.assertEqual(status, 409, body)

    def test_repin_rejects_races_before_saving_album(self) -> None:
        state = self.state()
        payload = self.repin_payload(state)
        before = self.album_path.read_bytes()
        from groove_serpent.album import repin_album_sides as real_repin

        def raced_repin(album: AlbumProject, album_path: Path, labels: list[str]) -> list[str]:
            project_path = self.directory / "side-a.groove.json"
            project = load_project(project_path)
            project.metadata["year"] = "2026"
            save_project(project, project_path)
            return real_repin(album, album_path, labels)

        with patch(
            "groove_serpent.album_review_server.repin_album_sides",
            side_effect=raced_repin,
        ):
            status, _headers, body = self.request("POST", "/api/album/repin", payload=payload)
        self.assertEqual(status, 409, body)
        self.assertEqual(self.album_path.read_bytes(), before)

    def test_repin_rejects_mid_operation_source_swap_as_conflict(self) -> None:
        state = self.state()
        payload = self.repin_payload(state)
        before = self.album_path.read_bytes()
        from groove_serpent.album import repin_album_sides as real_repin

        def raced_repin(album: AlbumProject, album_path: Path, labels: list[str]) -> list[str]:
            with (self.directory / "side-a.flac").open("ab") as handle:
                handle.write(b"changed")
            return real_repin(album, album_path, labels)

        with patch(
            "groove_serpent.album_review_server.repin_album_sides",
            side_effect=raced_repin,
        ):
            status, _headers, body = self.request("POST", "/api/album/repin", payload=payload)
        self.assertEqual(status, 409, body)
        self.assertEqual(self.album_path.read_bytes(), before)

    def test_repin_rechecks_album_digest_immediately_before_save(self) -> None:
        state = self.state()
        payload = self.repin_payload(state)
        from groove_serpent.album import repin_album_sides as real_repin

        def raced_repin(album: AlbumProject, album_path: Path, labels: list[str]) -> list[str]:
            result = real_repin(album, album_path, labels)
            external = load_album_project(album_path)
            external.metadata["external_change"] = "preserve me"
            save_album_project(external, album_path, overwrite=True)
            return result

        with patch(
            "groove_serpent.album_review_server.repin_album_sides",
            side_effect=raced_repin,
        ):
            status, _headers, body = self.request("POST", "/api/album/repin", payload=payload)
        self.assertEqual(status, 409, body)
        self.assertEqual(
            load_album_project(self.album_path).metadata["external_change"],
            "preserve me",
        )

    def test_post_rejects_foreign_origin_queries_and_non_exact_schema(self) -> None:
        state = self.state()
        payload = self.repin_payload(state)
        status, _headers, body = self.request(
            "POST",
            "/api/album/repin",
            payload=payload,
            headers={"Origin": "http://example.com"},
        )
        self.assertEqual(status, 403, body)

        status, _headers, body = self.request(
            "POST", "/api/album/repin?path=elsewhere", payload=payload
        )
        self.assertEqual(status, 400, body)

        for changed in (
            {**payload, "album_path": "elsewhere.json"},
            {key: value for key, value in payload.items() if key != "reviewed"},
            {**payload, "reviewed": False},
            {
                **payload,
                "expected_current_identity": {
                    **payload["expected_current_identity"],
                    "extra": "0" * 64,
                },
            },
        ):
            with self.subTest(changed=changed):
                status, _headers, body = self.request("POST", "/api/album/repin", payload=changed)
                self.assertEqual(status, 400, body)

    def test_post_enforces_json_media_type_body_limit_and_unique_fields(self) -> None:
        state = self.state()
        payload = self.repin_payload(state)
        encoded = json.dumps(payload).encode("utf-8")
        for body, headers, expected_status in (
            (encoded, {"Content-Type": "text/plain"}, 415),
            (b"", {"Content-Type": "application/json"}, 400),
            (b"[1]", {"Content-Type": "application/json"}, 400),
            (
                b'{"reviewed":true,"reviewed":true}',
                {"Content-Type": "application/json"},
                400,
            ),
        ):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
            request_headers = {
                "Authorization": self.server.session_auth.authorization_header,
                "Host": self.authority,
                "Origin": self.base,
                **headers,
            }
            connection.request(
                "POST",
                "/api/album/repin",
                body=body,
                headers=request_headers,
            )
            response = connection.getresponse()
            response_body = response.read()
            with self.subTest(body_length=len(body), headers=headers):
                self.assertEqual(response.status, expected_status, response_body)
            connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.putrequest("POST", "/api/album/repin", skip_host=True)
        connection.putheader("Host", self.authority)
        connection.putheader(
            "Authorization", self.server.session_auth.authorization_header
        )
        connection.putheader("Origin", self.base)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(64 * 1024 + 1))
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        self.assertEqual(response.status, 400, body)
        connection.close()

    def test_add_current_project_appends_an_explicitly_unpinned_side(self) -> None:
        side_c = self._write_project("side-c", "Third")
        state = self.state()
        payload = {
            **self.mutation_preconditions(state),
            "side_label": "C",
            "project_reference": side_c.name,
        }

        status, _headers, body = self.request("POST", "/api/album/add-side", payload=payload)

        self.assertEqual(status, 200, body)
        updated = json.loads(body)
        self.assertEqual(updated["album_revision"], state["album_revision"] + 1)
        added = updated["sides"][-1]
        self.assertEqual((added["order"], added["label"]), (3, "C"))
        self.assertEqual(added["project"], side_c.name)
        self.assertFalse(added["pinned"])
        self.assertIsNone(added["pin"])
        self.assertIn("side is unpinned", added["drift"])
        saved = load_album_project(self.album_path)
        self.assertEqual(saved.schema, "groove-serpent.album/3")
        self.assertIsNone(saved.sides[-1].pin)

    def test_add_rejects_collisions_escape_legacy_and_stale_source_without_write(
        self,
    ) -> None:
        side_c = self._write_project("side-c", "Third")
        state = self.state()
        base = self.mutation_preconditions(state)
        before = self.album_path.read_bytes()
        cases = (
            {**base, "side_label": "a", "project_reference": side_c.name},
            {
                **base,
                "side_label": "C",
                "project_reference": "SIDE-A.GROOVE.JSON",
            },
            {**base, "side_label": "C", "project_reference": "../side-c.groove.json"},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                status, _headers, body = self.request(
                    "POST", "/api/album/add-side", payload=payload
                )
                self.assertEqual(status, 400, body)
                self.assertEqual(self.album_path.read_bytes(), before)

        legacy = json.loads(side_c.read_text(encoding="utf-8"))
        legacy["schema_version"] = 3
        side_c.write_text(json.dumps(legacy), encoding="utf-8")
        payload = {**base, "side_label": "C", "project_reference": side_c.name}
        status, _headers, body = self.request("POST", "/api/album/add-side", payload=payload)
        self.assertEqual(status, 400, body)
        self.assertIn(b"migrate", body)
        self.assertEqual(self.album_path.read_bytes(), before)

        stale = self._write_project("side-stale", "Stale")
        (self.directory / "side-stale.flac").write_bytes(b"changed")
        payload = {**base, "side_label": "S", "project_reference": stale.name}
        status, _headers, body = self.request("POST", "/api/album/add-side", payload=payload)
        self.assertEqual(status, 400, body)
        self.assertIn(b"no longer matches", body)
        self.assertEqual(self.album_path.read_bytes(), before)

    def test_add_rejects_final_project_symlink_without_write(self) -> None:
        target = self._write_project("side-target", "Target")
        link = self.directory / "side-link.groove.json"
        try:
            link.symlink_to(target.name)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        state = self.state()
        before = self.album_path.read_bytes()
        payload = {
            **self.mutation_preconditions(state),
            "side_label": "C",
            "project_reference": link.name,
        }
        status, _headers, body = self.request("POST", "/api/album/add-side", payload=payload)
        self.assertEqual(status, 400, body)
        self.assertIn(b"symlink", body)
        self.assertEqual(self.album_path.read_bytes(), before)

    def test_remove_requires_typed_confirmation_and_unpins_resequenced_sides(
        self,
    ) -> None:
        state = self.state()
        base = {
            **self.mutation_preconditions(state),
            "side_label": "A",
        }
        before = self.album_path.read_bytes()
        status, _headers, body = self.request(
            "POST",
            "/api/album/remove-side",
            payload={**base, "confirmation": "remove A"},
        )
        self.assertEqual(status, 400, body)
        self.assertEqual(self.album_path.read_bytes(), before)

        status, _headers, body = self.request(
            "POST",
            "/api/album/remove-side",
            payload={**base, "confirmation": "REMOVE A"},
        )
        self.assertEqual(status, 200, body)
        updated = json.loads(body)
        self.assertEqual(len(updated["sides"]), 1)
        remaining = updated["sides"][0]
        self.assertEqual((remaining["label"], remaining["order"]), ("B", 1))
        self.assertFalse(remaining["pinned"])
        self.assertIsNone(load_album_project(self.album_path).sides[0].pin)

        final_state = self.state()
        status, _headers, body = self.request(
            "POST",
            "/api/album/remove-side",
            payload={
                **self.mutation_preconditions(final_state),
                "side_label": "B",
                "confirmation": "REMOVE B",
            },
        )
        self.assertEqual(status, 400, body)

    def test_reorder_preserves_side_identity_but_clears_every_pin(self) -> None:
        state = self.state()
        before_by_label = {side["label"]: side["current_identity"] for side in state["sides"]}
        payload = {
            **self.mutation_preconditions(state),
            "ordered_side_labels": ["B", "A"],
            "approval_acknowledged": True,
        }

        status, _headers, body = self.request("POST", "/api/album/reorder-sides", payload=payload)

        self.assertEqual(status, 200, body)
        updated = json.loads(body)
        self.assertTrue(updated["side_order_policy"]["approval_relevant"])
        self.assertEqual([side["label"] for side in updated["sides"]], ["B", "A"])
        self.assertEqual([side["order"] for side in updated["sides"]], [1, 2])
        self.assertTrue(all(side["pin"] is None for side in updated["sides"]))
        self.assertEqual(
            {side["label"]: side["current_identity"] for side in updated["sides"]},
            before_by_label,
        )

    def test_reorder_rejects_bad_set_missing_ack_and_stale_revision(self) -> None:
        state = self.state()
        base = self.mutation_preconditions(state)
        before = self.album_path.read_bytes()
        cases = (
            {
                **base,
                "ordered_side_labels": ["B", "A"],
                "approval_acknowledged": False,
            },
            {
                **base,
                "ordered_side_labels": ["A", "A"],
                "approval_acknowledged": True,
            },
        )
        for payload in cases:
            with self.subTest(payload=payload):
                status, _headers, body = self.request(
                    "POST", "/api/album/reorder-sides", payload=payload
                )
                self.assertEqual(status, 400, body)
                self.assertEqual(self.album_path.read_bytes(), before)

        stale = {
            **base,
            "expected_album_revision": state["album_revision"] + 1,
            "ordered_side_labels": ["B", "A"],
            "approval_acknowledged": True,
        }
        status, _headers, body = self.request("POST", "/api/album/reorder-sides", payload=stale)
        self.assertEqual(status, 409, body)
        self.assertEqual(self.album_path.read_bytes(), before)

    def test_update_details_hashes_artwork_and_preserves_side_pins(self) -> None:
        artwork = self.directory / "cover.png"
        artwork.write_bytes(b"\x89PNG\r\n\x1a\n" + b"safe-artwork")
        state = self.state()
        pins_before = [side["pin"] for side in state["sides"]]
        metadata = dict(state["metadata"])
        metadata.update(
            {
                "album": "Edited Album",
                "album_artist": "Edited Artist",
                "year": "2026",
                "genre": "Metal",
            }
        )
        payload = {
            **self.mutation_preconditions(state),
            "metadata": metadata,
            "artwork_path": artwork.name,
            "expected_artwork_sha256": None,
        }

        status, _headers, body = self.request("POST", "/api/album/update-details", payload=payload)

        self.assertEqual(status, 200, body)
        updated = json.loads(body)
        self.assertEqual(updated["metadata"]["album"], "Edited Album")
        self.assertEqual(updated["artwork"]["path"], artwork.name)
        self.assertEqual(updated["artwork"]["sha256"], sha256_file(artwork))
        self.assertEqual([side["pin"] for side in updated["sides"]], pins_before)

    def test_update_details_rejects_stale_artwork_and_side_drift(self) -> None:
        artwork = self.directory / "cover.png"
        artwork.write_bytes(b"\x89PNG\r\n\x1a\n" + b"safe-artwork")
        state = self.state()
        first_payload = {
            **self.mutation_preconditions(state),
            "metadata": dict(state["metadata"]),
            "artwork_path": artwork.name,
            "expected_artwork_sha256": None,
        }
        status, _headers, body = self.request(
            "POST", "/api/album/update-details", payload=first_payload
        )
        self.assertEqual(status, 200, body)
        updated = json.loads(body)
        artwork.write_bytes(b"\x89PNG\r\n\x1a\n" + b"changed")
        before = self.album_path.read_bytes()
        stale_payload = {
            **self.mutation_preconditions(updated),
            "metadata": {**updated["metadata"], "year": "2027"},
            "artwork_path": artwork.name,
            "expected_artwork_sha256": None,
        }
        status, _headers, body = self.request(
            "POST", "/api/album/update-details", payload=stale_payload
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(self.album_path.read_bytes(), before)

        artwork.write_bytes(b"\x89PNG\r\n\x1a\n" + b"safe-artwork")
        current = self.state()
        project = load_project(self.directory / "side-b.groove.json")
        project.metadata["year"] = "2030"
        save_project(project, self.directory / "side-b.groove.json")
        before = self.album_path.read_bytes()
        payload = {
            **self.mutation_preconditions(current),
            "metadata": {**current["metadata"], "year": "2028"},
            "artwork_path": artwork.name,
            "expected_artwork_sha256": None,
        }
        status, _headers, body = self.request("POST", "/api/album/update-details", payload=payload)
        self.assertEqual(status, 409, body)
        self.assertEqual(self.album_path.read_bytes(), before)

    def test_update_details_requires_exact_hash_for_downloaded_review_artwork(self) -> None:
        artwork = self.directory / "artwork" / "review" / "candidate.png"
        artwork.parent.mkdir(parents=True)
        artwork.write_bytes(b"\x89PNG\r\n\x1a\n" + b"reviewed-artwork")
        state = self.state()
        base = {
            **self.mutation_preconditions(state),
            "metadata": {**state["metadata"], "year": "2026"},
            "artwork_path": "artwork/review/candidate.png",
        }
        before = self.album_path.read_bytes()
        status, _headers, body = self.request(
            "POST",
            "/api/album/update-details",
            payload={**base, "expected_artwork_sha256": None},
        )
        self.assertEqual(status, 400, body)
        self.assertEqual(self.album_path.read_bytes(), before)

        status, _headers, body = self.request(
            "POST",
            "/api/album/update-details",
            payload={**base, "expected_artwork_sha256": "0" * 64},
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(self.album_path.read_bytes(), before)

        status, _headers, body = self.request(
            "POST",
            "/api/album/update-details",
            payload={**base, "expected_artwork_sha256": sha256_file(artwork)},
        )
        self.assertEqual(status, 200, body)
        updated = json.loads(body)
        self.assertEqual(updated["artwork"]["path"], "artwork/review/candidate.png")
        self.assertEqual(updated["artwork"]["sha256"], sha256_file(artwork))

    def test_update_details_rejects_escape_oversize_and_final_artwork_link(self) -> None:
        artwork = self.directory / "cover.png"
        artwork.write_bytes(b"\x89PNG\r\n\x1a\n" + b"safe-artwork")
        state = self.state()
        base = {
            **self.mutation_preconditions(state),
            "metadata": dict(state["metadata"]),
            "expected_artwork_sha256": None,
        }
        before = self.album_path.read_bytes()
        for payload in (
            {**base, "artwork_path": "../cover.png"},
            {
                **base,
                "metadata": {"album": "x" * 4097},
                "artwork_path": None,
            },
        ):
            with self.subTest(payload_keys=tuple(payload)):
                status, _headers, body = self.request(
                    "POST", "/api/album/update-details", payload=payload
                )
                self.assertEqual(status, 400, body)
                self.assertEqual(self.album_path.read_bytes(), before)

        link = self.directory / "linked-cover.png"
        try:
            link.symlink_to(artwork.name)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        status, _headers, body = self.request(
            "POST",
            "/api/album/update-details",
            payload={**base, "artwork_path": link.name},
        )
        self.assertEqual(status, 400, body)
        self.assertIn(b"symlink", body)
        self.assertEqual(self.album_path.read_bytes(), before)

    def test_invalid_host_is_rejected_and_internal_errors_are_generic(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.putrequest("GET", "/api/ping", skip_host=True)
        connection.putheader("Host", "example.com:80")
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        self.assertEqual(response.status, 400, body)
        connection.close()

        with patch(
            "groove_serpent.album_review_server.build_album_workbench_state",
            side_effect=RuntimeError("sensitive internal detail"),
        ):
            status, _headers, body = self.request("GET", "/api/album/state")
        self.assertEqual(status, 500, body)
        rendered = body.decode("utf-8")
        self.assertIn("Unexpected server error", rendered)
        self.assertNotIn("sensitive", rendered)

    def test_server_refuses_non_loopback_bind(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            AlbumReviewServer(("0.0.0.0", 0), self.album_path)

    def test_serve_album_opens_bootstrap_without_printing_capability(self) -> None:
        def timer(_delay: float, callback: object) -> SimpleNamespace:
            self.assertTrue(callable(callback))
            return SimpleNamespace(start=callback)

        with patch.object(
            AlbumReviewServer,
            "serve_forever",
            side_effect=KeyboardInterrupt,
        ), patch(
            "groove_serpent.album_review_server.threading.Timer",
            side_effect=timer,
        ), patch(
            "groove_serpent.album_review_server.webbrowser.open"
        ) as browser_open, patch("builtins.print") as printed:
            result = serve_album_project(self.album_path, open_browser=True)

        self.assertEqual(result, 0)
        browser_open.assert_called_once()
        browser_url = browser_open.call_args.args[0]
        parsed = urlsplit(browser_url)
        self.assertTrue(parsed.path.startswith("/__groove_serpent_session__/"))
        capability = parsed.path.rsplit("/", 1)[-1]
        rendered_output = "\n".join(
            " ".join(str(argument) for argument in call.args)
            for call in printed.call_args_list
        )
        self.assertNotIn(capability, rendered_output)

    def test_no_browser_album_prints_one_time_bootstrap_once(self) -> None:
        with patch.object(
            AlbumReviewServer,
            "serve_forever",
            side_effect=KeyboardInterrupt,
        ), patch(
            "groove_serpent.album_review_server.webbrowser.open"
        ) as browser_open, patch("builtins.print") as printed:
            result = serve_album_project(self.album_path, open_browser=False)

        self.assertEqual(result, 0)
        browser_open.assert_not_called()
        rendered_output = "\n".join(
            " ".join(str(argument) for argument in call.args)
            for call in printed.call_args_list
        )
        credential_line = next(
            line
            for line in rendered_output.splitlines()
            if line.startswith("One-time session bootstrap URL")
        )
        bootstrap_url = credential_line.rsplit(" ", 1)[-1]
        parsed = urlsplit(bootstrap_url)
        self.assertTrue(parsed.path.startswith("/__groove_serpent_session__/"))
        capability = parsed.path.rsplit("/", 1)[-1]
        self.assertEqual(rendered_output.count(capability), 1)


if __name__ == "__main__":
    unittest.main()
