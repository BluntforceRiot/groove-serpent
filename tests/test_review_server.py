from __future__ import annotations

import http.client
import ipaddress
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from groove_serpent.audio_snapshot import VerifiedAudioSnapshot
from groove_serpent.evidence import EvidenceRequestSuperseded
from groove_serpent.errors import ProjectValidationError
from groove_serpent.metadata import MetadataLookupError
from groove_serpent.media import sha256_file
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.project_io import load_project, save_project
from groove_serpent.review_server import (
    ReviewServer,
    _ipv4_server_endpoint,
    _stat_identity,
    serve_project,
)
from groove_serpent.recognition import RecognitionMatch, RecognitionReadiness


RELEASE_ID = "62d1c4ef-fc00-37af-8df7-485f6a31fcc4"
RELEASE_GROUP_ID = "0ef97d52-3f00-31bf-8413-f83ccb362675"
RECORDING_IDS = (
    "05df1765-62c0-4977-8959-bea4465e7e93",
    "1a97da55-54be-42d2-99f8-5c0d125c61bc",
)
TRACK_IDS = (
    "f02df099-2df0-37e3-b388-0eadc5175af3",
    "7d941494-fc67-3326-9041-4022bde81f49",
)


class ReviewServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.source_path = self.directory / "side.flac"
        self.source_path.write_bytes(bytes(range(256)) * 8)
        stat = self.source_path.stat()
        project = Project(
            source=AudioSource(
                path=self.source_path.name,
                filename=self.source_path.name,
                size_bytes=stat.st_size,
                modified_ns=stat.st_mtime_ns,
                duration_seconds=10.0,
                sample_rate=1_000,
                channels=2,
                codec_name="flac",
                bits_per_raw_sample=16,
                sample_format="s16",
                sample_count=10_000,
                sha256=sha256_file(self.source_path),
            ),
            settings=AnalysisSettings(),
            analysis=AnalysisSummary(
                music_start_seconds=1.0,
                music_end_seconds=9.0,
                noise_floor_db=-50.0,
                silence_threshold_db=-44.0,
                active_threshold_db=-32.0,
                envelope_window_seconds=0.05,
                waveform=[0.1, 0.8, 0.2],
            ),
            tracks=[
                Track(
                    number=1,
                    title="First",
                    start_sample=1_000,
                    end_sample=5_000,
                    start_seconds=1.0,
                    end_seconds=5.0,
                ),
                Track(
                    number=2,
                    title="Second",
                    start_sample=5_000,
                    end_sample=9_000,
                    start_seconds=5.0,
                    end_seconds=9.0,
                ),
            ],
        )
        self.project_path = self.directory / "side.groove.json"
        save_project(project, self.project_path)

        self.server = ReviewServer(("127.0.0.1", 0), self.project_path)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.port = self.server.server_address[1]
        self.authority = f"{self.server.session_auth.public_host}:{self.port}"
        self.base = self.server.session_auth.origin(port=self.port)
        self.authenticated_opener = urllib.request.build_opener()
        self.authenticated_opener.addheaders = [
            ("Authorization", self.server.session_auth.authorization_header)
        ]

    def tearDown(self) -> None:
        snapshot_path = self.server.source_snapshot.path
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(snapshot_path.exists())
        self.temporary_directory.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        add_state_receipt: bool = True,
        authenticate: bool = True,
    ) -> tuple[int, http.client.HTTPMessage, bytes, bool]:
        state_bound_paths = {
            "/api/save",
            "/api/metadata/apply",
            "/api/export",
            "/api/recognition/identify",
        }
        if add_state_receipt and method == "POST" and path in state_bound_paths and body:
            try:
                payload = json.loads(body)
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            required_identity = {"expected_revision", "expected_project_sha256"}
            if path in {"/api/export", "/api/recognition/identify"}:
                required_identity.add("expected_source_receipt")
            if isinstance(payload, dict) and not required_identity.issubset(payload):
                state = json.load(self.authenticated_opener.open(self.base + "/api/project"))
                payload.setdefault("expected_revision", state["revision"])
                payload.setdefault("expected_project_sha256", state["project_sha256"])
                if path in {"/api/export", "/api/recognition/identify"}:
                    payload.setdefault(
                        "expected_source_receipt",
                        state["source_receipt"]["receipt"],
                    )
            if isinstance(payload, dict) and path == "/api/export":
                payload.setdefault("output_dir", "")
                payload.setdefault("formats", ["flac", "m4a"])
            if isinstance(payload, dict):
                body = json.dumps(payload).encode("utf-8")
        request_headers = dict(headers or {})
        request_headers.setdefault("Host", self.authority)
        if authenticate:
            request_headers.setdefault(
                "Authorization", self.server.session_auth.authorization_header
            )
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        response_body = response.read()
        result = (response.status, response.headers, response_body, response.will_close)
        connection.close()
        return result

    def test_normalized_request_targets_are_rejected_before_bootstrap(self) -> None:
        bootstrap = self.server.session_auth.bootstrap_path
        for target in (f"/{bootstrap}", f"///{bootstrap.lstrip('/')}", "//app.js"):
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

        status, headers, body, _will_close = self.request(
            "GET", bootstrap, authenticate=False
        )
        self.assertEqual(status, 303, body)
        self.assertIsNotNone(headers.get("Set-Cookie"))

    def test_session_auth_bootstrap_bearer_cookie_and_native_mutation(self) -> None:
        authorization = self.server.session_auth.authorization_header
        token = authorization.removeprefix("Bearer ")
        self.assertGreaterEqual(len(token), 43)
        self.assertRegex(
            self.server.session_auth.public_host,
            r"^groove-serpent-[0-9a-f]{32}\.localhost$",
        )
        bootstrap_nonce = self.server.session_auth.bootstrap_path.rsplit("/", 1)[-1]
        self.assertNotEqual(token, bootstrap_nonce)

        for path in ("/", "/app.js", "/styles.css"):
            status, _headers, _body, _will_close = self.request(
                "GET", path, authenticate=False
            )
            self.assertEqual(status, 200)

        status, _headers, _body, _will_close = self.request(
            "GET", "/api/project", authenticate=False
        )
        self.assertEqual(status, 401)
        status, _headers, _body, _will_close = self.request(
            "GET", "/audio", authenticate=False
        )
        self.assertEqual(status, 401)
        status, _headers, _body, _will_close = self.request(
            "POST",
            "/api/save",
            body=b"{}",
            headers={"Content-Type": "application/json"},
            authenticate=False,
        )
        self.assertEqual(status, 401)

        status, headers, body, _will_close = self.request(
            "GET",
            self.server.session_auth.bootstrap_path,
            authenticate=False,
        )
        self.assertEqual(status, 303, body)
        self.assertEqual(headers["Location"], "/")
        cookie_header = headers["Set-Cookie"]
        self.assertIn("; Path=/; HttpOnly; SameSite=Strict", cookie_header)
        cookie = cookie_header.split(";", 1)[0]
        self.assertEqual(cookie.split("=", 1)[1], token)

        status, _headers, _body, _will_close = self.request(
            "GET",
            self.server.session_auth.bootstrap_path,
            authenticate=False,
        )
        self.assertEqual(status, 401)

        status, replay_headers, replay_body, _will_close = self.request(
            "GET",
            self.server.session_auth.bootstrap_path,
            headers={"Cookie": cookie},
            authenticate=False,
        )
        self.assertEqual(status, 303, replay_body)
        self.assertEqual(replay_headers["Location"], "/")
        self.assertIsNone(replay_headers.get("Set-Cookie"))

        status, _headers, body, _will_close = self.request(
            "GET",
            "/api/project",
            headers={"Cookie": cookie},
            authenticate=False,
        )
        self.assertEqual(status, 200, body)
        self.assertNotIn(token.encode("ascii"), body)
        state = json.loads(body)

        state["tracks"][0]["title"] = "Authenticated cookie edit"
        mutation = {
            "metadata": state["metadata"],
            "tracks": state["tracks"],
            "expected_revision": state["revision"],
            "expected_project_sha256": state["project_sha256"],
        }
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/save",
            body=json.dumps(mutation).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
            },
            authenticate=False,
        )
        self.assertEqual(status, 403, body)
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/save",
            body=json.dumps(mutation).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "Origin": self.base,
            },
            authenticate=False,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(
            load_project(self.project_path).tracks[0].title,
            mutation["tracks"][0]["title"],
        )

        status, _headers, body, _will_close = self.request(
            "GET",
            "/api/project",
            headers={"Cookie": cookie},
            authenticate=False,
        )
        self.assertEqual(status, 200, body)
        state = json.loads(body)
        state["tracks"][0]["title"] = "Authenticated native edit"
        mutation = {
            "metadata": state["metadata"],
            "tracks": state["tracks"],
            "expected_revision": state["revision"],
            "expected_project_sha256": state["project_sha256"],
        }
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/save",
            body=json.dumps(mutation).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(
            load_project(self.project_path).tracks[0].title,
            mutation["tracks"][0]["title"],
        )

        for alias in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            with self.subTest(alias=alias):
                status, _headers, _body, _will_close = self.request(
                    "GET",
                    "/api/project",
                    headers={"Host": alias},
                )
                self.assertEqual(status, 400)

    def test_session_auth_rejects_wrong_malformed_and_duplicate_credentials(self) -> None:
        for headers in (
            {"Authorization": "Bearer wrong"},
            {"Authorization": self.server.session_auth.authorization_header, "Cookie": "broken"},
        ):
            with self.subTest(headers=tuple(headers)):
                status, _response_headers, _body, _will_close = self.request(
                    "GET", "/api/project", headers=headers, authenticate=False
                )
                self.assertEqual(status, 401)

        status, bootstrap_headers, _body, _will_close = self.request(
            "GET", self.server.session_auth.bootstrap_path, authenticate=False
        )
        self.assertEqual(status, 303)
        status, _headers, _body, _will_close = self.request(
            "GET",
            f"{self.server.session_auth.bootstrap_path}?unexpected=1",
            authenticate=False,
        )
        self.assertEqual(status, 401)
        cookie = bootstrap_headers["Set-Cookie"].split(";", 1)[0]
        status, _headers, _body, _will_close = self.request(
            "GET",
            "/api/project",
            headers={"Cookie": f"{cookie}; {cookie}"},
            authenticate=False,
        )
        self.assertEqual(status, 401)

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.putrequest("GET", "/api/project", skip_host=True)
        connection.putheader("Host", self.authority)
        connection.putheader("Authorization", self.server.session_auth.authorization_header)
        connection.putheader("Authorization", self.server.session_auth.authorization_header)
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 401)
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.putrequest("GET", "/api/project", skip_host=True)
        connection.putheader("Host", self.authority)
        connection.putheader("Cookie", cookie)
        connection.putheader("Cookie", cookie)
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 401)
        connection.close()

    def test_bootstrap_nonce_is_consumed_once_under_concurrency(self) -> None:
        secondary = ReviewServer(("127.0.0.1", 0), self.project_path)
        secondary_thread = threading.Thread(
            target=secondary.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        secondary_thread.start()
        barrier = threading.Barrier(8)

        def attempt_bootstrap() -> tuple[int, str | None]:
            barrier.wait(timeout=3)
            connection = http.client.HTTPConnection(
                "127.0.0.1", secondary.server_port, timeout=3
            )
            connection.request(
                "GET",
                secondary.session_auth.bootstrap_path,
                headers={
                    "Host": (
                        f"{secondary.session_auth.public_host}:"
                        f"{secondary.server_port}"
                    )
                },
            )
            response = connection.getresponse()
            response.read()
            result = response.status, response.headers.get("Set-Cookie")
            connection.close()
            return result

        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(lambda _index: attempt_bootstrap(), range(8)))
            successes = [item for item in results if item[0] == 303]
            self.assertEqual(len(successes), 1, results)
            self.assertEqual(sum(item[0] == 401 for item in results), 7, results)
            self.assertIsNotNone(successes[0][1])

            connection = http.client.HTTPConnection(
                "127.0.0.1", secondary.server_port, timeout=3
            )
            connection.request(
                "GET",
                secondary.session_auth.bootstrap_path,
                headers={
                    "Host": (
                        f"{secondary.session_auth.public_host}:"
                        f"{secondary.server_port}"
                    )
                },
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 401)
            connection.close()
        finally:
            secondary.shutdown()
            secondary.server_close()
            secondary_thread.join(timeout=2)

    def test_project_audio_range_and_save(self) -> None:
        payload = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        self.assertEqual(payload["tracks"][0]["title"], "First")
        self.assertNotIn("project_path", payload)
        self.assertEqual(payload["revision"], 1)
        self.assertEqual(len(payload["project_sha256"]), 64)
        self.assertEqual(payload["project_sha256"], sha256_file(self.project_path))
        self.assertEqual(payload["source_receipt"]["sha256"], sha256_file(self.source_path))
        self.assertTrue(payload["source_receipt"]["receipt"])

        range_request = urllib.request.Request(
            self.base + "/audio", headers={"Range": "bytes=10-29"}
        )
        with self.authenticated_opener.open(range_request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(len(response.read()), 20)

        payload["tracks"][0]["title"] = "Edited First"
        save_request = urllib.request.Request(
            self.base + "/api/save",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps(
                {
                    "metadata": payload["metadata"],
                    "tracks": payload["tracks"],
                    "expected_revision": payload["revision"],
                    "expected_project_sha256": payload["project_sha256"],
                }
            ).encode("utf-8"),
        )
        saved = json.load(self.authenticated_opener.open(save_request))
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["revision"], 2)
        self.assertNotEqual(saved["project_sha256"], payload["project_sha256"])
        self.assertEqual(saved["project"]["tracks"][0]["title"], "Edited First")
        self.assertEqual(saved["project"]["edit_history"][-1]["action"], "edit_track")
        persisted = load_project(self.project_path)
        self.assertEqual(persisted.tracks[0].title, "Edited First")
        self.assertEqual(persisted.edit_history[-1].after_sha256, persisted.state_sha256)

    def test_continuous_context_route_accepts_distinct_crackle_kind(self) -> None:
        state = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        expected_context = {
            "kind": "crackle",
            "method_contract": {
                "proposal_schema": "groove-serpent.continuous-crackle-proposal/1"
            },
        }
        request_payload = {
            "action": "read-exact-continuous-preview-context",
            "expected_revision": state["revision"],
            "expected_project_sha256": state["project_sha256"],
            "expected_source_receipt": state["source_receipt"]["receipt"],
            "kind": "crackle",
        }
        with patch(
            "groove_serpent.review_server.current_continuous_preview_context",
            return_value=expected_context,
        ) as current:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/restoration/continuous/context",
                body=json.dumps(request_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 200, body)
        response = json.loads(body)
        self.assertEqual(response["context"], expected_context)
        current.assert_called_once()
        called_project, called_kind = current.call_args.args
        self.assertTrue(Path(called_project).samefile(self.project_path))
        self.assertEqual(called_kind, "crackle")

    def test_checkpoint_endpoint_persists_exact_named_state(self) -> None:
        state = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/checkpoint",
            body=json.dumps(
                {
                    "name": "Ready to export",
                    "expected_revision": state["revision"],
                    "expected_project_sha256": state["project_sha256"],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        payload = json.loads(body)["project"]
        self.assertEqual(payload["revision"], 2)
        self.assertEqual(payload["checkpoints"][0]["name"], "Ready to export")
        self.assertEqual(
            payload["checkpoints"][0]["state_sha256"],
            payload["analyzer_baseline"]["state_sha256"],
        )

        original = self.project_path.read_bytes()
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/checkpoint",
            body=json.dumps(
                {
                    "name": True,
                    "expected_revision": payload["revision"],
                    "expected_project_sha256": payload["project_sha256"],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400, body)
        self.assertEqual(self.project_path.read_bytes(), original)

    def test_topology_proposal_and_apply_are_revision_bound_and_reversible(self) -> None:
        state = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        release_tracks = [
            {"position": 1, "number": "1", "title": "One"},
            {"position": 2, "number": "2", "title": "Two"},
            {"position": 3, "number": "3", "title": "Three"},
        ]
        details = {
            "id": RELEASE_ID,
            "selections": [
                {
                    "key": "medium:1:all",
                    "label": "Complete release",
                    "track_count": 3,
                    "tracks": release_tracks,
                }
            ],
        }
        self.server.musicbrainz_client.get_release = lambda release_id: details
        fake_proposal = {"schema": "groove-serpent.topology-proposal/1", "tracks": [{}, {}, {}]}
        with patch(
            "groove_serpent.review_server.propose_topology_refit",
            return_value=fake_proposal,
        ) as propose:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/topology/propose",
                body=json.dumps(
                    {
                        "release_id": RELEASE_ID,
                        "selection_key": "medium:1:all",
                        "expected_revision": state["revision"],
                        "expected_project_sha256": state["project_sha256"],
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(status, 200, body)
        proposal_payload = json.loads(body)
        self.assertEqual(proposal_payload["proposed_track_count"], 3)
        self.assertEqual(propose.call_args.args[1], release_tracks)

        original_tracks = load_project(self.project_path).tracks
        replacement = [
            Track(
                number=1,
                title="One",
                start_sample=1_000,
                end_sample=3_000,
                start_seconds=1.0,
                end_seconds=3.0,
            ),
            Track(
                number=2,
                title="Two",
                start_sample=3_000,
                end_sample=5_000,
                start_seconds=3.0,
                end_seconds=5.0,
            ),
            Track(
                number=3,
                title="Three",
                start_sample=5_000,
                end_sample=9_000,
                start_seconds=5.0,
                end_seconds=9.0,
            ),
        ]
        with patch(
            "groove_serpent.review_server.tracks_from_topology_proposal",
            return_value=replacement,
        ) as apply_proposal:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/topology/apply",
                body=json.dumps(
                    {
                        "proposal": fake_proposal,
                        "expected_revision": state["revision"],
                        "expected_project_sha256": state["project_sha256"],
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(status, 200, body)
        applied = json.loads(body)["project"]
        self.assertEqual(len(applied["tracks"]), 3)
        self.assertEqual(applied["edit_history"][-1]["action"], "topology_refit")
        self.assertEqual(len(applied["edit_history"][-1]["before"]["tracks"]), 2)
        self.assertEqual(len(applied["edit_history"][-1]["after"]["tracks"]), 3)
        apply_proposal.assert_called_once()
        self.assertEqual(len(original_tracks), 2)

    def test_save_rejects_boolean_and_fractional_sample_markers(self) -> None:
        project_payload = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        original_bytes = self.project_path.read_bytes()
        for value in (True, 999.9, "1000"):
            with self.subTest(value=value):
                payload = {
                    "metadata": project_payload["metadata"],
                    "tracks": [dict(item) for item in project_payload["tracks"]],
                }
                payload["tracks"][0]["start_sample"] = value
                status, _headers, body, _will_close = self.request(
                    "POST",
                    "/api/save",
                    body=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400)
                self.assertIn("JSON integers", json.loads(body)["error"])
                self.assertEqual(self.project_path.read_bytes(), original_bytes)

    def test_stale_tab_save_returns_conflict_without_writing(self) -> None:
        first_tab = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        first_tracks = [dict(item) for item in first_tab["tracks"]]
        first_tracks[0]["title"] = "First tab"
        first_request = {
            "metadata": first_tab["metadata"],
            "tracks": first_tracks,
            "expected_revision": first_tab["revision"],
            "expected_project_sha256": first_tab["project_sha256"],
        }
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/save",
            body=json.dumps(first_request).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        after_first = self.project_path.read_bytes()

        stale_tracks = [dict(item) for item in first_tab["tracks"]]
        stale_tracks[0]["title"] = "Stale tab"
        stale_request = {
            "metadata": first_tab["metadata"],
            "tracks": stale_tracks,
            "expected_revision": first_tab["revision"],
            "expected_project_sha256": first_tab["project_sha256"],
        }
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/save",
            body=json.dumps(stale_request).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 409, body)
        self.assertIn("changed", json.loads(body)["error"].lower())
        self.assertEqual(self.project_path.read_bytes(), after_first)
        self.assertEqual(load_project(self.project_path).tracks[0].title, "First tab")

    def test_mutations_require_strict_project_state_receipts(self) -> None:
        state = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        original_bytes = self.project_path.read_bytes()
        cases = [
            (None, None),
            (True, state["project_sha256"]),
            (1.0, state["project_sha256"]),
            ("1", state["project_sha256"]),
            (state["revision"], True),
            (state["revision"], "bad"),
        ]
        for revision, digest in cases:
            with self.subTest(revision=revision, digest=digest):
                request_payload = {
                    "metadata": state["metadata"],
                    "tracks": state["tracks"],
                }
                if revision is not None:
                    request_payload["expected_revision"] = revision
                if digest is not None:
                    request_payload["expected_project_sha256"] = digest
                status, _headers, _body, _will_close = self.request(
                    "POST",
                    "/api/save",
                    body=json.dumps(request_payload).encode(),
                    headers={"Content-Type": "application/json"},
                    add_state_receipt=False,
                )
                self.assertEqual(status, 400)
                self.assertEqual(self.project_path.read_bytes(), original_bytes)

    def test_same_size_source_swap_disables_payload_playback_and_save(self) -> None:
        state = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        original_project = self.project_path.read_bytes()
        old_stat = self.source_path.stat()
        self.source_path.write_bytes(b"x" * old_stat.st_size)
        os.utime(
            self.source_path,
            ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000),
        )

        for path in ("/api/project", "/audio"):
            with self.subTest(path=path):
                status, _headers, body, _will_close = self.request("GET", path)
                self.assertEqual(status, 400)
                self.assertIn("source audio changed", json.loads(body)["error"].lower())

        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/save",
            body=json.dumps(
                {
                    "metadata": state["metadata"],
                    "tracks": state["tracks"],
                    "expected_revision": state["revision"],
                    "expected_project_sha256": state["project_sha256"],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400, body)
        self.assertEqual(self.project_path.read_bytes(), original_project)

    def test_source_verification_cache_rehashes_only_after_file_state_changes(self) -> None:
        first = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        from groove_serpent.review_server import _sha256_handle

        with patch(
            "groove_serpent.review_server._sha256_handle", wraps=_sha256_handle
        ) as hasher:
            second = json.load(self.authenticated_opener.open(self.base + "/api/project"))
            self.assertEqual(hasher.call_count, 0)
            stat = self.source_path.stat()
            os.utime(
                self.source_path,
                ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000),
            )
            third = json.load(self.authenticated_opener.open(self.base + "/api/project"))
            self.assertEqual(hasher.call_count, 1)
        self.assertEqual(
            first["source_receipt"]["receipt"],
            second["source_receipt"]["receipt"],
        )
        self.assertNotEqual(
            second["source_receipt"]["receipt"],
            third["source_receipt"]["receipt"],
        )

    def test_repeated_audio_ranges_use_only_snapshot_identity_leases(self) -> None:
        from groove_serpent.review_server import _source_probe_signature

        expected = self.source_path.read_bytes()
        snapshot_path = self.server.source_snapshot.path
        snapshot_identity = snapshot_path.stat()
        ranges = ((10, 29), (100, 143), (1_500, 1_599))

        with patch(
            "groove_serpent.review_server._sha256_handle",
            side_effect=AssertionError(
                "playback must not hash the complete live source"
            ),
        ), patch(
            "groove_serpent.audio_snapshot.assert_file_receipt",
            side_effect=AssertionError(
                "playback must not hash the complete session snapshot"
            ),
        ), patch(
            "groove_serpent.review_server._source_probe_signature",
            wraps=_source_probe_signature,
        ) as bounded_probe:
            for start, end in ranges:
                with self.subTest(start=start, end=end):
                    status, headers, body, _will_close = self.request(
                        "GET",
                        "/audio",
                        headers={"Range": f"bytes={start}-{end}"},
                    )
                    self.assertEqual(status, 206, body)
                    self.assertEqual(body, expected[start : end + 1])
                    self.assertEqual(
                        headers["Content-Range"],
                        f"bytes {start}-{end}/{len(expected)}",
                    )

        self.assertEqual(bounded_probe.call_count, len(ranges))
        self.assertEqual(self.server.source_snapshot.path, snapshot_path)
        self.assertEqual(snapshot_path.stat().st_ino, snapshot_identity.st_ino)

    def test_review_session_start_fully_authenticates_source_and_snapshot(self) -> None:
        from groove_serpent.publication import (
            assert_file_receipt,
            capture_verified_copy,
        )

        with patch(
            "groove_serpent.audio_snapshot.capture_verified_copy",
            wraps=capture_verified_copy,
        ) as source_capture, patch(
            "groove_serpent.audio_snapshot.assert_file_receipt",
            wraps=assert_file_receipt,
        ) as snapshot_verifier:
            secondary = ReviewServer(("127.0.0.1", 0), self.project_path)
            secondary_snapshot = secondary.source_snapshot.path
            try:
                self.assertEqual(source_capture.call_count, 1)
                self.assertEqual(snapshot_verifier.call_count, 1)
            finally:
                secondary.server_close()

        self.assertFalse(secondary_snapshot.exists())

    def test_audio_range_rejects_restored_mtime_snapshot_tamper_cheaply(self) -> None:
        snapshot_path = self.server.source_snapshot.path
        captured = snapshot_path.stat()
        with snapshot_path.open("r+b") as handle:
            handle.seek(captured.st_size // 2)
            original = handle.read(1)
            handle.seek(captured.st_size // 2)
            handle.write(b"x" if original != b"x" else b"y")
            handle.flush()
            os.fsync(handle.fileno())
        os.utime(
            snapshot_path,
            ns=(captured.st_atime_ns, captured.st_mtime_ns),
        )

        with patch(
            "groove_serpent.review_server._sha256_handle",
            side_effect=AssertionError("tamper rejection must remain a cheap check"),
        ), patch(
            "groove_serpent.audio_snapshot.assert_file_receipt",
            side_effect=AssertionError("tamper rejection must remain a cheap check"),
        ):
            status, _headers, body, _will_close = self.request("GET", "/audio")

        self.assertEqual(status, 400, body)
        self.assertIn("snapshot lease changed", json.loads(body)["error"].lower())

    def test_audio_range_rejects_restored_mtime_live_source_tamper_cheaply(
        self,
    ) -> None:
        captured = self.source_path.stat()
        with self.source_path.open("r+b") as handle:
            handle.seek(captured.st_size // 2)
            original = handle.read(1)
            handle.seek(captured.st_size // 2)
            handle.write(b"x" if original != b"x" else b"y")
            handle.flush()
            os.fsync(handle.fileno())
        os.utime(
            self.source_path,
            ns=(captured.st_atime_ns, captured.st_mtime_ns),
        )

        with patch(
            "groove_serpent.review_server._sha256_handle",
            side_effect=AssertionError("tamper rejection must remain a cheap check"),
        ), patch(
            "groove_serpent.audio_snapshot.assert_file_receipt",
            side_effect=AssertionError("tamper rejection must remain a cheap check"),
        ):
            status, _headers, body, _will_close = self.request("GET", "/audio")

        self.assertEqual(status, 400, body)
        self.assertIn("source audio changed", json.loads(body)["error"].lower())

    def test_project_mutation_retains_full_source_revalidation(self) -> None:
        state = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        from groove_serpent.review_server import _sha256_handle

        with patch(
            "groove_serpent.review_server._sha256_handle",
            wraps=_sha256_handle,
        ) as hasher:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/checkpoint",
                body=json.dumps(
                    {
                        "name": "Hash-bound checkpoint",
                        "expected_revision": state["revision"],
                        "expected_project_sha256": state["project_sha256"],
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(status, 200, body)
        self.assertEqual(hasher.call_count, 2)

    def test_source_stat_identity_allows_absent_platform_fields(self) -> None:
        portable_stat = SimpleNamespace(
            st_dev=22,
            st_ino=11,
            st_size=123,
            st_mtime_ns=2_000_000_000,
            st_ctime_ns=3_000_000_000,
        )

        identity = _stat_identity(portable_stat)

        self.assertEqual(identity[:3], (22, 11, 123))
        self.assertEqual(identity[-2:], (None, None))

    def test_source_cache_refuses_same_size_replacement_with_restored_mtime(self) -> None:
        initial = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        old_stat = self.source_path.stat()
        replacement = bytes(reversed(self.source_path.read_bytes()))
        self.assertEqual(len(replacement), old_stat.st_size)
        self.source_path.write_bytes(replacement)
        os.utime(
            self.source_path,
            ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns),
        )

        status, _headers, body, _will_close = self.request("GET", "/api/project")
        self.assertEqual(status, 400, body)
        self.assertIn("source audio changed", json.loads(body)["error"].lower())
        self.assertNotEqual(sha256_file(self.source_path), initial["source_receipt"]["sha256"])

    def test_save_can_split_a_track_and_keeps_submitted_metadata_with_it(self) -> None:
        payload = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        first, second = payload["tracks"]
        split_tracks = [
            {
                **first,
                "number": 88,
                "title": "First half",
                "end_sample": 3_000,
                "confidence": 0.91,
                "expected_duration_seconds": 2.0,
                "musicbrainz_recording_id": RECORDING_IDS[0],
                "musicbrainz_track_id": TRACK_IDS[0],
            },
            {
                **first,
                "number": 89,
                "title": "Second half",
                "start_sample": 3_000,
                "artist": "Split Artist",
                "confidence": 0.73,
                "expected_duration_seconds": 2.25,
                "musicbrainz_recording_id": "",
                "musicbrainz_track_id": "",
            },
            {
                **second,
                "number": 90,
                "title": "Original second",
                "artist": "Second Artist",
                "musicbrainz_recording_id": RECORDING_IDS[1],
                "musicbrainz_track_id": TRACK_IDS[1],
            },
        ]
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/save",
            body=json.dumps(
                {"metadata": payload["metadata"], "tracks": split_tracks}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)

        saved = load_project(self.project_path)
        self.assertEqual(len(saved.tracks), 3)
        self.assertEqual([track.number for track in saved.tracks], [1, 2, 3])
        self.assertEqual(
            [(track.start_sample, track.end_sample) for track in saved.tracks],
            [(1_000, 3_000), (3_000, 5_000), (5_000, 9_000)],
        )
        self.assertEqual(saved.tracks[1].artist, "Split Artist")
        self.assertEqual(saved.tracks[1].expected_duration_seconds, 2.25)
        self.assertEqual(
            saved.tracks[2].musicbrainz_recording_id,
            RECORDING_IDS[1],
        )
        self.assertEqual(saved.tracks[2].musicbrainz_track_id, TRACK_IDS[1])

    def test_save_can_merge_tracks_and_renumbers_the_submission(self) -> None:
        payload = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        merged = {
            **payload["tracks"][0],
            "number": 42,
            "title": "Merged track",
            "end_sample": 9_000,
            "musicbrainz_recording_id": RECORDING_IDS[1],
            "musicbrainz_track_id": TRACK_IDS[1],
        }
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/save",
            body=json.dumps(
                {"metadata": payload["metadata"], "tracks": [merged]}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)

        saved = load_project(self.project_path)
        self.assertEqual(len(saved.tracks), 1)
        self.assertEqual(saved.tracks[0].number, 1)
        self.assertEqual(saved.tracks[0].title, "Merged track")
        self.assertEqual(saved.tracks[0].start_sample, 1_000)
        self.assertEqual(saved.tracks[0].end_sample, 9_000)
        self.assertEqual(saved.tracks[0].musicbrainz_track_id, TRACK_IDS[1])

    def test_invalid_structural_saves_leave_the_project_byte_unchanged(self) -> None:
        payload = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        original_bytes = self.project_path.read_bytes()
        first, second = payload["tracks"]
        cases = {
            "empty": [],
            "gap": [first, {**second, "start_sample": 5_001}],
            "overlap": [first, {**second, "start_sample": 4_999}],
            "past source": [first, {**second, "end_sample": 10_001}],
        }
        for label, tracks in cases.items():
            with self.subTest(label=label):
                status, _headers, _body, _will_close = self.request(
                    "POST",
                    "/api/save",
                    body=json.dumps(
                        {"metadata": payload["metadata"], "tracks": tracks}
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400)
                self.assertEqual(self.project_path.read_bytes(), original_bytes)

    def test_save_strictly_validates_submitted_track_values(self) -> None:
        payload = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        original_bytes = self.project_path.read_bytes()
        cases = {
            "confidence text": ("confidence", "0.5"),
            "confidence boolean": ("confidence", True),
            "confidence non-finite": ("confidence", float("nan")),
            "expected duration zero": ("expected_duration_seconds", 0),
            "expected duration boolean": ("expected_duration_seconds", False),
            "expected duration overflow": ("expected_duration_seconds", 10**400),
            "title not text": ("title", ["First"]),
            "MBID not text": ("musicbrainz_recording_id", 123),
            "MBID invalid": ("musicbrainz_track_id", "not-a-uuid"),
        }
        for label, (field, value) in cases.items():
            with self.subTest(label=label):
                tracks = [dict(item) for item in payload["tracks"]]
                tracks[0][field] = value
                status, _headers, _body, _will_close = self.request(
                    "POST",
                    "/api/save",
                    body=json.dumps(
                        {"metadata": payload["metadata"], "tracks": tracks}
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400)
                self.assertEqual(self.project_path.read_bytes(), original_bytes)

    def test_save_requires_exact_top_level_and_per_track_keys(self) -> None:
        state = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        original_bytes = self.project_path.read_bytes()
        base = {
            "metadata": state["metadata"],
            "tracks": [dict(item) for item in state["tracks"]],
            "expected_revision": state["revision"],
            "expected_project_sha256": state["project_sha256"],
        }
        cases: list[dict[str, object]] = []
        unknown_top = dict(base)
        unknown_top["metdata"] = {}
        cases.append(unknown_top)
        missing_top = dict(base)
        missing_top.pop("metadata")
        cases.append(missing_top)
        unknown_track = {**base, "tracks": [dict(item) for item in state["tracks"]]}
        unknown_track["tracks"][0]["titel"] = "typo"
        cases.append(unknown_track)
        missing_track = {**base, "tracks": [dict(item) for item in state["tracks"]]}
        missing_track["tracks"][0].pop("title")
        cases.append(missing_track)

        for payload in cases:
            with self.subTest(payload=payload):
                status, _headers, body, _will_close = self.request(
                    "POST",
                    "/api/save",
                    body=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                    add_state_receipt=False,
                )
                self.assertEqual(status, 400, body)
                self.assertRegex(
                    json.loads(body)["error"].lower(),
                    "unsupported fields|missing required fields",
                )
                self.assertEqual(self.project_path.read_bytes(), original_bytes)

    def test_rejects_non_loopback_bind_and_allows_safe_localhost(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            ReviewServer(("0.0.0.0", 0), self.project_path)

        localhost_server = ReviewServer(("localhost", 0), self.project_path)
        try:
            endpoint = _ipv4_server_endpoint(localhost_server)
            self.assertEqual(endpoint[1], localhost_server.server_port)
            self.assertTrue(
                ipaddress.ip_address(endpoint[0]).is_loopback
            )
        finally:
            localhost_server.server_close()

    def test_serve_rejects_out_of_range_port_before_server_creation(self) -> None:
        for invalid in (-1, 65_536, 10**400, True):
            with self.subTest(port=invalid), self.assertRaisesRegex(
                ProjectValidationError, "0 to 65535"
            ):
                serve_project(self.project_path, port=invalid, open_browser=False)

    def test_serve_opens_secret_bootstrap_without_printing_the_capability(self) -> None:
        def timer(_delay: float, callback: object) -> SimpleNamespace:
            self.assertTrue(callable(callback))
            return SimpleNamespace(start=callback)

        with patch.object(
            ReviewServer,
            "serve_forever",
            side_effect=KeyboardInterrupt,
        ), patch(
            "groove_serpent.review_server.threading.Timer",
            side_effect=timer,
        ), patch(
            "groove_serpent.review_server.webbrowser.open"
        ) as browser_open, patch("builtins.print") as printed:
            result = serve_project(self.project_path, open_browser=True)

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

    def test_no_browser_prints_one_time_bootstrap_credential_once(self) -> None:
        with patch.object(
            ReviewServer,
            "serve_forever",
            side_effect=KeyboardInterrupt,
        ), patch(
            "groove_serpent.review_server.webbrowser.open"
        ) as browser_open, patch("builtins.print") as printed:
            result = serve_project(self.project_path, open_browser=False)

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

    def test_rejects_host_that_is_not_loopback_on_the_actual_port(self) -> None:
        hostile_hosts = [
            f"example.com:{self.port}",
            f"127.0.0.1:{self.port + 1}",
        ]
        for host in hostile_hosts:
            with self.subTest(host=host):
                status, _headers, body, will_close = self.request(
                    "GET", "/api/ping", headers={"Host": host}
                )
                self.assertEqual(status, 400)
                self.assertTrue(will_close)
                self.assertEqual(json.loads(body)["error"], "Invalid Host header.")

    def test_post_requires_json_and_same_origin_and_rejects_transfer_encoding(self) -> None:
        cases = [
            ({"Content-Type": "text/plain"}, 415),
            (
                {
                    "Content-Type": "application/json",
                    "Origin": f"http://localhost:{self.port}",
                },
                403,
            ),
            (
                {
                    "Content-Type": "application/json",
                    "Transfer-Encoding": "chunked",
                },
                400,
            ),
        ]
        for headers, expected_status in cases:
            with self.subTest(headers=headers):
                status, response_headers, _body, will_close = self.request(
                    "POST", "/api/save", body=b"{}", headers=headers
                )
                self.assertEqual(status, expected_status)
                self.assertEqual(response_headers["Connection"], "close")
                self.assertTrue(will_close)

    def test_accepts_matching_origin(self) -> None:
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/save",
            body=b"{}",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Origin": self.base,
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("missing required fields", json.loads(body)["error"])

    def test_export_overwrite_requires_a_boolean_before_export(self) -> None:
        with patch("groove_serpent.review_server.export_project") as exporter:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/export",
                body=json.dumps({"overwrite": "false"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(status, 400)
        self.assertIn("boolean", json.loads(body)["error"])
        exporter.assert_not_called()

    def test_export_flac_compression_requires_a_json_integer(self) -> None:
        for value in (True, 8.0, "8"):
            with self.subTest(value=value), patch(
                "groove_serpent.review_server.export_project"
            ) as exporter:
                status, _headers, body, _will_close = self.request(
                    "POST",
                    "/api/export",
                    body=json.dumps({"flac_compression": value}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400, body)
                self.assertIn("integer", json.loads(body)["error"].lower())
                exporter.assert_not_called()

    def test_export_speed_factor_is_strict_and_forwarded(self) -> None:
        for value in (True, "1.039", 0.24, 2.01):
            with self.subTest(value=value), patch(
                "groove_serpent.review_server.export_project"
            ) as exporter:
                status, _headers, body, _will_close = self.request(
                    "POST",
                    "/api/export",
                    body=json.dumps({"source_speed_factor": value}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400, body)
                self.assertIn("speed factor", json.loads(body)["error"].lower())
                exporter.assert_not_called()

        report = type(
            "Report",
            (),
            {"output_directory": "out", "manifest_path": "manifest.json", "files": []},
        )()
        with patch(
            "groove_serpent.review_server.export_project", return_value=report
        ) as exporter:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/export",
                body=json.dumps({"source_speed_factor": 1.039482143}).encode(),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(status, 200, body)
        self.assertEqual(exporter.call_args.kwargs["source_speed_factor"], 1.039482143)

        with patch(
            "groove_serpent.review_server.export_project", return_value=report
        ) as exporter:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/export",
                body=json.dumps({"source_speed_factor": 0.425930658}).encode(),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(status, 200, body)
        self.assertEqual(exporter.call_args.kwargs["source_speed_factor"], 0.425930658)

    def test_export_requires_exact_identity_and_refuses_stale_clean_tab(self) -> None:
        stale = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        output_dir = self.directory / "stale-must-not-publish"
        missing_identity = {
            "output_dir": str(output_dir),
            "formats": ["flac"],
        }
        with patch("groove_serpent.review_server.export_project") as exporter:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/export",
                body=json.dumps(missing_identity).encode(),
                headers={"Content-Type": "application/json"},
                add_state_receipt=False,
            )
        self.assertEqual(status, 400, body)
        exporter.assert_not_called()

        identity_only = {
            "expected_revision": stale["revision"],
            "expected_project_sha256": stale["project_sha256"],
            "expected_source_receipt": stale["source_receipt"]["receipt"],
        }
        with patch("groove_serpent.review_server.export_project") as exporter:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/export",
                body=json.dumps(identity_only).encode(),
                headers={"Content-Type": "application/json"},
                add_state_receipt=False,
            )
        self.assertEqual(status, 400, body)
        self.assertIn("formats", json.loads(body)["error"].lower())
        self.assertIn("output_dir", json.loads(body)["error"].lower())
        exporter.assert_not_called()

        changed_tracks = [dict(item) for item in stale["tracks"]]
        changed_tracks[0]["title"] = "Concurrent reviewed edit"
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/save",
            body=json.dumps(
                {
                    "metadata": stale["metadata"],
                    "tracks": changed_tracks,
                    "expected_revision": stale["revision"],
                    "expected_project_sha256": stale["project_sha256"],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)

        stale_export = {
            **missing_identity,
            "expected_revision": stale["revision"],
            "expected_project_sha256": stale["project_sha256"],
            "expected_source_receipt": stale["source_receipt"]["receipt"],
        }
        with patch("groove_serpent.review_server.export_project") as exporter:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/export",
                body=json.dumps(stale_export).encode(),
                headers={"Content-Type": "application/json"},
                add_state_receipt=False,
            )
        self.assertEqual(status, 409, body)
        self.assertIn("changed", json.loads(body)["error"].lower())
        exporter.assert_not_called()
        self.assertFalse(output_dir.exists())

    def test_export_rejects_unknown_fields_and_non_strict_aac_bitrate(self) -> None:
        invalid_bitrates = (True, 256, "256", "31k", "513k", "256K", " 256k")
        for value in invalid_bitrates:
            with self.subTest(value=value), patch(
                "groove_serpent.review_server.export_project"
            ) as exporter:
                status, _headers, body, _will_close = self.request(
                    "POST",
                    "/api/export",
                    body=json.dumps({"aac_bitrate": value}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400, body)
                self.assertIn("aac bitrate", json.loads(body)["error"].lower())
                exporter.assert_not_called()

        with patch("groove_serpent.review_server.export_project") as exporter:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/export",
                body=json.dumps({"unexpected": True}).encode(),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(status, 400, body)
        self.assertIn("unsupported fields", json.loads(body)["error"].lower())
        exporter.assert_not_called()

        report = type(
            "Report",
            (),
            {"output_directory": "out", "manifest_path": "manifest.json", "files": []},
        )()
        with patch(
            "groove_serpent.review_server.export_project", return_value=report
        ) as exporter:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/export",
                body=json.dumps({"aac_bitrate": "320k"}).encode(),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(status, 200, body)
        self.assertEqual(exporter.call_args.kwargs["aac_bitrate"], "320k")

    def test_evidence_endpoint_uses_strict_exact_samples_and_receipts(self) -> None:
        evidence = {
            "schema": "groove-serpent.evidence-window/1",
            "selection": {"start_sample": 100, "end_sample_exclusive": 900},
        }
        with patch(
            "groove_serpent.review_server.analyze_evidence_window",
            return_value=evidence,
        ) as analyzer, patch(
            "groove_serpent.review_server._sha256_handle",
            side_effect=AssertionError("evidence must not hash the whole source"),
        ), patch(
            "groove_serpent.audio_snapshot.assert_file_receipt",
            side_effect=AssertionError("evidence must not hash the snapshot"),
        ):
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/evidence",
                body=json.dumps(
                    {"start_sample": 100, "end_sample": 900, "focus_sample": 500}
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["project_revision"], 1)
        self.assertEqual(payload["project_sha256"], sha256_file(self.project_path))
        self.assertTrue(payload["source_receipt"]["receipt"])
        self.assertEqual(analyzer.call_args.kwargs["start_sample"], 100)
        self.assertEqual(analyzer.call_args.kwargs["end_sample"], 900)
        self.assertEqual(analyzer.call_args.kwargs["focus_sample"], 500)
        snapshot = analyzer.call_args.kwargs["source_snapshot"]
        self.assertIsInstance(snapshot, VerifiedAudioSnapshot)
        self.assertNotEqual(snapshot.path, self.source_path)
        self.assertEqual(snapshot.path, self.server.source_snapshot.path)
        self.assertEqual(snapshot.path.read_bytes(), self.source_path.read_bytes())

        for request_payload in (
            {"start_sample": True, "end_sample": 900, "focus_sample": 500},
            {"start_sample": 100, "end_sample": 900},
            {"start_sample": 100, "end_sample": 900, "focus_sample": 500, "extra": 1},
        ):
            with self.subTest(request_payload=request_payload), patch(
                "groove_serpent.review_server.analyze_evidence_window"
            ) as analyzer:
                status, _headers, body, _will_close = self.request(
                    "POST",
                    "/api/evidence",
                    body=json.dumps(request_payload).encode(),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400, body)
                analyzer.assert_not_called()

    def test_evidence_cache_uses_exact_key_and_returns_fresh_payloads(self) -> None:
        def evidence_payload(*_args: object, **kwargs: object) -> dict[str, object]:
            return {
                "schema": "groove-serpent.evidence-window/1",
                "selection": {
                    "start_sample": kwargs["start_sample"],
                    "end_sample_exclusive": kwargs["end_sample"],
                },
                "mutable": [],
            }

        with patch(
            "groove_serpent.review_server.analyze_evidence_window",
            side_effect=evidence_payload,
        ) as analyzer:
            request = {
                "start_sample": 100,
                "end_sample": 900,
                "focus_sample": 500,
            }
            first = self.request(
                "POST",
                "/api/evidence",
                body=json.dumps(request).encode(),
                headers={"Content-Type": "application/json"},
            )
            second = self.request(
                "POST",
                "/api/evidence",
                body=json.dumps(request).encode(),
                headers={"Content-Type": "application/json"},
            )
            request["focus_sample"] = 501
            third = self.request(
                "POST",
                "/api/evidence",
                body=json.dumps(request).encode(),
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(first[0], 200, first[2])
        self.assertEqual(second[0], 200, second[2])
        self.assertEqual(third[0], 200, third[2])
        self.assertEqual(analyzer.call_count, 2)
        self.assertEqual(json.loads(first[2])["mutable"], [])
        self.assertEqual(json.loads(second[2])["mutable"], [])

    def test_newest_evidence_request_cancels_older_decode_without_global_lock(
        self,
    ) -> None:
        first_started = threading.Event()
        first_cancelled = threading.Event()
        call_lock = threading.Lock()
        call_count = 0

        def analyze(*_args: object, **kwargs: object) -> dict[str, object]:
            nonlocal call_count
            with call_lock:
                call_count += 1
                call_number = call_count
            cancelled = kwargs["cancelled"]
            assert callable(cancelled)
            if call_number == 1:
                first_started.set()
                self.assertTrue(first_cancelled.wait(timeout=2.0))
                raise EvidenceRequestSuperseded("superseded")
            self.assertTrue(cancelled is not None)
            first_cancelled.set()
            return {
                "schema": "groove-serpent.evidence-window/1",
                "selection": {"start_sample": kwargs["start_sample"]},
            }

        first_result: list[tuple[int, http.client.HTTPMessage, bytes, bool]] = []

        def first_request() -> None:
            first_result.append(
                self.request(
                    "POST",
                    "/api/evidence",
                    body=json.dumps(
                        {
                            "start_sample": 100,
                            "end_sample": 900,
                            "focus_sample": 500,
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                )
            )

        with patch(
            "groove_serpent.review_server.analyze_evidence_window",
            side_effect=analyze,
        ):
            thread = threading.Thread(target=first_request)
            thread.start()
            self.assertTrue(first_started.wait(timeout=2.0))
            second = self.request(
                "POST",
                "/api/evidence",
                body=json.dumps(
                    {
                        "start_sample": 200,
                        "end_sample": 800,
                        "focus_sample": 501,
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(second[0], 200, second[2])
        self.assertTrue(first_result)
        self.assertEqual(first_result[0][0], 409, first_result[0][2])
        self.assertEqual(call_count, 2)

    def test_server_log_suppresses_only_expected_client_disconnects(self) -> None:
        with patch(
            "groove_serpent.review_server.ThreadingHTTPServer.handle_error"
        ) as fallback:
            for error in (
                BrokenPipeError(),
                ConnectionAbortedError(),
                ConnectionResetError(),
            ):
                with self.subTest(error=type(error).__name__):
                    try:
                        raise error
                    except type(error):
                        self.server.handle_error(object(), ("127.0.0.1", 1))
            fallback.assert_not_called()

            try:
                raise RuntimeError("unexpected")
            except RuntimeError:
                self.server.handle_error(object(), ("127.0.0.1", 1))
            fallback.assert_called_once()

    def test_borrowed_session_snapshot_keeps_precommit_assertions_enabled(self) -> None:
        project = load_project(self.project_path)
        borrowed, _receipt = self.server.verified_source_snapshot(project)
        tampered = self.directory / "tampered-session-snapshot.flac"
        tampered.write_bytes(borrowed.path.read_bytes() + b"tampered")

        with self.assertRaisesRegex(
            ProjectValidationError, "staged source audio snapshot changed"
        ):
            replace(borrowed, path=tampered).assert_snapshot_unchanged()

        changed_live = self.directory / "changed-live-source.flac"
        changed_live.write_bytes(borrowed.live_path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ProjectValidationError, "changed during"):
            replace(borrowed, live_path=changed_live).assert_live_unchanged()

    def test_recognition_track_number_requires_a_json_integer(self) -> None:
        with patch.object(
            self.server.recognition_provider, "identify_track"
        ) as identify:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/recognition/identify",
                body=json.dumps({"track_number": 1}).encode(),
                headers={"Content-Type": "application/json"},
                add_state_receipt=False,
            )
        self.assertEqual(status, 400, body)
        self.assertIn("expected_source_receipt", json.loads(body)["error"])
        identify.assert_not_called()

        for value in (True, 1.0, "1"):
            with self.subTest(value=value), patch.object(
                self.server.recognition_provider, "identify_track"
            ) as identify:
                status, _headers, body, _will_close = self.request(
                    "POST",
                    "/api/recognition/identify",
                    body=json.dumps({"track_number": value}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400, body)
                self.assertIn("integer", json.loads(body)["error"].lower())
                identify.assert_not_called()

    def test_invalid_and_oversized_bodies_close_the_connection(self) -> None:
        cases = [
            (b"{", {"Content-Type": "application/json"}),
            (
                None,
                {
                    "Content-Type": "application/json",
                    "Content-Length": "2000001",
                },
            ),
        ]
        for body, headers in cases:
            with self.subTest(headers=headers):
                status, response_headers, _body, will_close = self.request(
                    "POST", "/api/save", body=body, headers=headers
                )
                self.assertEqual(status, 400)
                self.assertEqual(response_headers["Connection"], "close")
                self.assertTrue(will_close)

    def test_empty_suffix_range_is_unsatisfiable(self) -> None:
        status, headers, body, _will_close = self.request(
            "GET", "/audio", headers={"Range": "bytes=-"}
        )
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], f"bytes */{self.source_path.stat().st_size}")
        self.assertEqual(body, b"")

    def test_security_headers_are_present_on_success_and_error_responses(self) -> None:
        for path, expected_status in [("/api/ping", 200), ("/missing", 404)]:
            with self.subTest(path=path):
                status, headers, _body, _will_close = self.request("GET", path)
                self.assertEqual(status, expected_status)
                self.assertEqual(
                    headers["Content-Security-Policy"],
                    "default-src 'self'; img-src 'self' data:; media-src 'self'; "
                    "frame-ancestors 'none'",
                )
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(
                    headers["Cross-Origin-Resource-Policy"], "same-origin"
                )

        for path in ["/audio"]:
            with self.subTest(path=path):
                status, headers, _body, _will_close = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "private, no-store")

    def test_unexpected_errors_return_a_generic_500(self) -> None:
        with patch(
            "groove_serpent.review_server.load_project_with_sha256",
            side_effect=RuntimeError("sensitive implementation detail"),
        ):
            status, _headers, body, _will_close = self.request("GET", "/api/project")
        self.assertEqual(status, 500)
        self.assertEqual(
            json.loads(body),
            {"ok": False, "error": "Unexpected server error."},
        )

    def test_release_lookup_apply_and_local_artwork(self) -> None:
        release = {
            "id": RELEASE_ID,
            "title": "Matched Album",
            "artist": "Matched Artist",
            "date": "1984-03-01",
            "country": "US",
            "status": "Official",
            "barcode": "123456",
            "label": "Test Label",
            "catalog_number": "CAT-1",
            "release_group_id": RELEASE_GROUP_ID,
            "genres": ["new wave"],
            "formats": ['12" Vinyl'],
            "track_count": 4,
            "has_artwork": True,
            "selections": [
                {
                    "key": "medium:1:side:A",
                    "kind": "side",
                    "label": "Side A",
                    "medium_position": 1,
                    "format": '12" Vinyl',
                    "side": "A",
                    "track_count": 2,
                    "tracks": [
                        {
                            "title": "Matched One",
                            "artist": "Matched Artist",
                            "duration_seconds": 240.0,
                            "recording_id": RECORDING_IDS[0],
                            "track_id": TRACK_IDS[0],
                        },
                        {
                            "title": "Matched Two",
                            "artist": "Matched Artist",
                            "duration_seconds": 245.0,
                            "recording_id": RECORDING_IDS[1],
                            "track_id": TRACK_IDS[1],
                        },
                    ],
                },
                {
                    "key": "medium:1:all",
                    "kind": "medium",
                    "label": "Complete release",
                    "medium_position": 1,
                    "side": None,
                    "track_count": 4,
                    "tracks": [],
                },
            ],
        }
        self.server.musicbrainz_client.search_releases = lambda artist, album: [
            {"id": RELEASE_ID, "title": album, "artist": artist}
        ]
        self.server.musicbrainz_client.get_release = lambda release_id: dict(release)

        def download_art(_release_id: str, *, size: str) -> dict[str, object]:
            artwork = self.directory / "artwork" / "cover.jpg"
            artwork.parent.mkdir()
            artwork.write_bytes(b"\xff\xd8\xfftest")
            return {
                "relative_path": "artwork/cover.jpg",
                "source_url": "https://coverartarchive.org/release/test/front",
                "mime_type": "image/jpeg",
                "sha256": sha256_file(artwork),
                "size_bytes": artwork.stat().st_size,
                "selected_size": size,
            }

        self.server.cover_art_client.download_front_art = download_art

        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/metadata/search",
            body=json.dumps({"artist": "Matched Artist", "album": "Matched Album"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["results"][0]["id"], RELEASE_ID)

        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/metadata/release",
            body=json.dumps({"release_id": RELEASE_ID}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        selections = json.loads(body)["release"]["selections"]
        self.assertEqual(len(selections), 2)
        self.assertEqual(selections[0]["key"], "medium:1:side:A")

        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/metadata/apply",
            body=json.dumps(
                {
                    "release_id": RELEASE_ID,
                    "selection_key": "medium:1:side:A",
                    "download_artwork": True,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        response = json.loads(body)
        self.assertEqual(response["project"]["tracks"][0]["title"], "Matched One")
        saved = load_project(self.project_path)
        self.assertEqual(saved.metadata["musicbrainz_release_id"], RELEASE_ID)
        self.assertEqual(saved.tracks[1].musicbrainz_recording_id, RECORDING_IDS[1])

        status, headers, body, _will_close = self.request("GET", "/artwork")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertEqual(headers["Cache-Control"], "private, no-store")
        self.assertTrue(body.startswith(b"\xff\xd8\xff"))

        release_without_art = dict(release)
        release_without_art["title"] = "New Release Without Art"
        release_without_art["has_artwork"] = False
        release_without_art["release_group_id"] = ""
        self.server.musicbrainz_client.get_release = lambda release_id: dict(
            release_without_art
        )
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/metadata/apply",
            body=json.dumps(
                {
                    "release_id": RELEASE_ID,
                    "selection_key": "medium:1:side:A",
                    "download_artwork": True,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        saved = load_project(self.project_path)
        self.assertNotIn("cover_art_path", saved.metadata)
        status, _headers, _body, _will_close = self.request("GET", "/artwork")
        self.assertEqual(status, 404)

    def test_artwork_endpoint_refuses_bytes_that_do_not_match_stored_hash(self) -> None:
        artwork = self.directory / "artwork" / "cover.jpg"
        artwork.parent.mkdir()
        artwork.write_bytes(b"\xff\xd8\xffverified-cover")
        project = load_project(self.project_path)
        before = project.capture_state()
        project.metadata.update(
            {
                "cover_art_path": "artwork/cover.jpg",
                "cover_art_sha256": sha256_file(artwork),
                "cover_art_mime_type": "image/jpeg",
                "cover_art_size_bytes": str(artwork.stat().st_size),
            }
        )
        project.append_history(
            action="edit_metadata",
            summary="Attached verified cover for server test",
            before=before,
            after=project.capture_state(),
        )
        save_project(project, self.project_path)

        status, headers, body, _will_close = self.request("GET", "/artwork")
        self.assertEqual(status, 200, body)
        self.assertEqual(headers["Content-Type"], "image/jpeg")

        artwork.write_bytes(b"\xff\xd8\xffchanged!-cover")
        self.assertEqual(artwork.stat().st_size, len(b"\xff\xd8\xffverified-cover"))
        status, _headers, body, _will_close = self.request("GET", "/artwork")
        self.assertEqual(status, 400, body)
        self.assertIn("artwork changed", json.loads(body)["error"].lower())

    def test_stale_metadata_apply_returns_conflict_before_artwork_write(self) -> None:
        stale = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        changed_tracks = [dict(item) for item in stale["tracks"]]
        changed_tracks[0]["title"] = "Concurrent edit"
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/save",
            body=json.dumps(
                {
                    "metadata": stale["metadata"],
                    "tracks": changed_tracks,
                    "expected_revision": stale["revision"],
                    "expected_project_sha256": stale["project_sha256"],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        after_concurrent_edit = self.project_path.read_bytes()
        release = {
            "id": RELEASE_ID,
            "title": "Stale Album",
            "artist": "Stale Artist",
            "release_group_id": RELEASE_GROUP_ID,
            "has_artwork": True,
            "selections": [
                {
                    "key": "medium:1:all",
                    "kind": "medium",
                    "label": "Complete release",
                    "medium_position": 1,
                    "side": None,
                    "track_count": 2,
                    "tracks": [
                        {"title": "One", "artist": "Stale Artist"},
                        {"title": "Two", "artist": "Stale Artist"},
                    ],
                }
            ],
        }
        self.server.musicbrainz_client.get_release = lambda release_id: dict(release)
        artwork_calls: list[str] = []

        def unexpected_artwork(release_id: str, *, size: str) -> dict[str, object]:
            artwork_calls.append(release_id)
            raise AssertionError("stale metadata must not write artwork")

        self.server.cover_art_client.download_front_art = unexpected_artwork
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/metadata/apply",
            body=json.dumps(
                {
                    "release_id": RELEASE_ID,
                    "selection_key": "medium:1:all",
                    "download_artwork": True,
                    "expected_revision": stale["revision"],
                    "expected_project_sha256": stale["project_sha256"],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(artwork_calls, [])
        self.assertEqual(self.project_path.read_bytes(), after_concurrent_edit)

    def test_metadata_apply_uses_release_group_artwork_fallback(self) -> None:
        release = {
            "id": RELEASE_ID,
            "title": "Fallback Album",
            "artist": "Fallback Artist",
            "date": "2019-03-29",
            "country": "US",
            "status": "Official",
            "barcode": "123456",
            "label": "Test Label",
            "catalog_number": "CAT-2",
            "release_group_id": RELEASE_GROUP_ID,
            "genres": ["metal"],
            "has_artwork": False,
            "selections": [
                {
                    "key": "medium:1:all",
                    "kind": "medium",
                    "label": "Complete release",
                    "medium_position": 1,
                    "side": None,
                    "track_count": 2,
                    "tracks": [
                        {
                            "title": "Fallback One",
                            "artist": "Fallback Artist",
                            "recording_id": RECORDING_IDS[0],
                            "track_id": TRACK_IDS[0],
                        },
                        {
                            "title": "Fallback Two",
                            "artist": "Fallback Artist",
                            "recording_id": RECORDING_IDS[1],
                            "track_id": TRACK_IDS[1],
                        },
                    ],
                }
            ],
        }
        self.server.musicbrainz_client.get_release = lambda release_id: dict(release)
        release_calls: list[tuple[str, str]] = []
        group_calls: list[tuple[str, str]] = []

        def download_release_art(release_id: str, *, size: str) -> dict[str, object]:
            release_calls.append((release_id, size))
            raise MetadataLookupError("Cover Art Archive request failed (HTTP 404).")

        def download_group_art(group_id: str, *, size: str) -> dict[str, object]:
            group_calls.append((group_id, size))
            artwork = self.directory / "artwork" / "group-cover.jpg"
            artwork.parent.mkdir(exist_ok=True)
            artwork.write_bytes(b"\xff\xd8\xffgroup")
            return {
                "relative_path": "artwork/group-cover.jpg",
                "source_url": "https://coverartarchive.org/release/group/front",
                "mime_type": "image/jpeg",
                "sha256": "a" * 64,
                "size_bytes": artwork.stat().st_size,
                "selected_size": size,
            }

        self.server.cover_art_client.download_front_art = download_release_art
        self.server.cover_art_client.download_release_group_front_art = (
            download_group_art
        )

        def apply_release() -> dict[str, object]:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/metadata/apply",
                body=json.dumps(
                    {
                        "release_id": RELEASE_ID,
                        "selection_key": "medium:1:all",
                        "download_artwork": True,
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 200, body)
            return json.loads(body)

        response = apply_release()
        self.assertEqual(response["warning"], "")
        self.assertEqual(release_calls, [])
        self.assertEqual(group_calls, [(RELEASE_GROUP_ID, "1200")])

        release["has_artwork"] = True
        response = apply_release()
        self.assertEqual(response["warning"], "")
        self.assertEqual(release_calls, [(RELEASE_ID, "1200")])
        self.assertEqual(
            group_calls,
            [(RELEASE_GROUP_ID, "1200"), (RELEASE_GROUP_ID, "1200")],
        )
        saved = load_project(self.project_path)
        self.assertEqual(saved.metadata["cover_art_path"], "artwork/group-cover.jpg")
        self.assertEqual(
            saved.metadata["musicbrainz_release_group_id"], RELEASE_GROUP_ID
        )

    def test_recognition_status_and_track_identification(self) -> None:
        class FakeRecognitionProvider:
            def readiness(self) -> RecognitionReadiness:
                return RecognitionReadiness(
                    provider="fake", enabled=True, ready=True, message="Ready for tests."
                )

            def identify_track(
                self, source, start, end, rate, *, source_speed_factor=1.0
            ):
                self.speed_factor = source_speed_factor
                self.call = (source, start, end, rate)
                return [
                    RecognitionMatch(
                        title="Identified Song",
                        artist_credit="Identified Artist",
                        score=0.98,
                        recording_mbid=RECORDING_IDS[0],
                    )
                ]

        provider = FakeRecognitionProvider()
        self.server.recognition_provider = provider
        status, _headers, body, _will_close = self.request(
            "GET", "/api/recognition/status"
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ready"])
        state = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        recognition_request = {
            "track_number": 1,
            "expected_revision": state["revision"],
            "expected_project_sha256": state["project_sha256"],
            "expected_source_receipt": state["source_receipt"]["receipt"],
        }

        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/recognition/identify",
            body=json.dumps(recognition_request).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        match = json.loads(body)["matches"][0]
        self.assertEqual(match["title"], "Identified Song")
        self.assertEqual(provider.call[1:], (1_000, 5_000, 1_000))
        self.assertIsInstance(provider.call[0], VerifiedAudioSnapshot)
        self.assertNotEqual(provider.call[0].path, self.source_path)
        self.assertEqual(provider.call[0].path, self.server.source_snapshot.path)
        self.assertEqual(
            provider.call[0].path.read_bytes(),
            self.source_path.read_bytes(),
        )
        recognition_response = json.loads(body)
        track_region = recognition_response["track_region"]
        self.assertEqual(track_region["track_number"], 1)
        self.assertEqual(track_region["start_sample"], 1_000)
        self.assertEqual(track_region["end_sample_exclusive"], 5_000)
        self.assertEqual(track_region["sample_rate"], 1_000)
        self.assertEqual(track_region["requested_speed_factor"], 1.0)
        self.assertEqual(track_region["fingerprint_asetrate_hz"], 1_000)
        self.assertEqual(track_region["fingerprint_effective_speed_factor"], 1.0)
        self.assertEqual(
            track_region["fingerprint_speed_transform"],
            "integer-asetrate-pitch-and-tempo/1",
        )
        self.assertEqual(len(track_region["speed_state_sha256"]), 64)
        self.assertEqual(provider.speed_factor, 1.0)
        self.assertEqual(recognition_response["project_sha256"], state["project_sha256"])

        self.source_path.write_bytes(b"x" * self.source_path.stat().st_size)
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/recognition/identify",
            body=json.dumps(recognition_request).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("source audio changed", json.loads(body)["error"].lower())

    def test_recognition_swap_and_restore_never_redirects_provider_input(self) -> None:
        original = self.source_path.read_bytes()

        class SwappingProvider:
            def identify_track(
                self, source, start, end, rate, *, source_speed_factor=1.0
            ):
                del start, end, rate, source_speed_factor
                self.received = source
                self_live = self_outer.source_path
                self_live.write_bytes(b"x" * len(original))
                try:
                    self_outer.assertEqual(source.path.read_bytes(), original)
                finally:
                    self_live.write_bytes(original)
                return []

        self_outer = self
        provider = SwappingProvider()
        self.server.recognition_provider = provider
        state = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/recognition/identify",
            body=json.dumps(
                {
                    "track_number": 1,
                    "expected_revision": state["revision"],
                    "expected_project_sha256": state["project_sha256"],
                    "expected_source_receipt": state["source_receipt"]["receipt"],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            add_state_receipt=False,
        )
        self.assertEqual(status, 409, body)
        self.assertIsInstance(provider.received, VerifiedAudioSnapshot)
        self.assertNotEqual(provider.received.path, self.source_path)
        self.assertEqual(self.source_path.read_bytes(), original)

    def test_recognition_refuses_a_stale_track_region_before_provider_call(self) -> None:
        stale = json.load(self.authenticated_opener.open(self.base + "/api/project"))
        changed_tracks = [dict(item) for item in stale["tracks"]]
        changed_tracks[0]["end_sample"] = 4_500
        changed_tracks[1]["start_sample"] = 4_500
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/save",
            body=json.dumps(
                {
                    "metadata": stale["metadata"],
                    "tracks": changed_tracks,
                    "expected_revision": stale["revision"],
                    "expected_project_sha256": stale["project_sha256"],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)

        with patch.object(
            self.server.recognition_provider, "identify_track"
        ) as identify:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/recognition/identify",
                body=json.dumps(
                    {
                        "track_number": 1,
                        "expected_revision": stale["revision"],
                        "expected_project_sha256": stale["project_sha256"],
                        "expected_source_receipt": stale["source_receipt"]["receipt"],
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
                add_state_receipt=False,
            )
        self.assertEqual(status, 409, body)
        self.assertIn("changed", json.loads(body)["error"].lower())
        identify.assert_not_called()

    def test_complete_release_apply_preserves_per_track_sides(self) -> None:
        project = load_project(self.project_path)
        project.tracks[0].side = "A"
        project.tracks[1].side = "B"
        save_project(project, self.project_path)
        release = {
            "id": RELEASE_ID,
            "title": "Matched Album",
            "artist": "Matched Artist",
            "date": "2025-10-24",
            "release_group_id": RELEASE_GROUP_ID,
            "genres": [],
            "has_artwork": False,
            "selections": [
                {
                    "key": "medium:1:all",
                    "kind": "medium",
                    "label": "Complete release",
                    "medium_position": 1,
                    "side": None,
                    "track_count": 2,
                    "tracks": [
                        {"title": "Matched One", "artist": "Matched Artist"},
                        {"title": "Matched Two", "artist": "Matched Artist"},
                    ],
                }
            ],
        }
        self.server.musicbrainz_client.get_release = lambda release_id: dict(release)

        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/metadata/apply",
            body=json.dumps(
                {
                    "release_id": RELEASE_ID,
                    "selection_key": "medium:1:all",
                    "download_artwork": False,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        saved = load_project(self.project_path)
        self.assertEqual([track.side for track in saved.tracks], ["A", "B"])

        before_clear = saved.capture_state()
        for track in saved.tracks:
            track.side = ""
        saved.append_history(
            action="edit_track",
            summary="Cleared side labels for test setup",
            before=before_clear,
            after=saved.capture_state(),
        )
        save_project(saved, self.project_path)
        release["selections"][0]["tracks"][0]["side"] = "A"
        release["selections"][0]["tracks"][1]["side"] = "B"
        status, _headers, body, _will_close = self.request(
            "POST",
            "/api/metadata/apply",
            body=json.dumps(
                {
                    "release_id": RELEASE_ID,
                    "selection_key": "medium:1:all",
                    "download_artwork": False,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        populated = load_project(self.project_path)
        self.assertEqual([track.side for track in populated.tracks], ["A", "B"])

    def test_recognition_rejects_overlapping_work(self) -> None:
        class UnexpectedProvider:
            def identify_track(
                self, source, start, end, rate, *, source_speed_factor=1.0
            ):
                del source_speed_factor
                raise AssertionError("provider must not run while the lock is held")

        self.server.recognition_provider = UnexpectedProvider()
        self.server.recognition_lock.acquire()
        try:
            status, _headers, body, _will_close = self.request(
                "POST",
                "/api/recognition/identify",
                body=json.dumps({"track_number": 1}).encode(),
                headers={"Content-Type": "application/json"},
            )
        finally:
            self.server.recognition_lock.release()
        self.assertEqual(status, 400)
        self.assertIn("already running", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
