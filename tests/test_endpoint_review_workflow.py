from __future__ import annotations

import http.client
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from groove_serpent import __version__
import groove_serpent.endpoint_proposals as endpoint_module
from groove_serpent.cli import main
from groove_serpent.endpoint_proposals import EndpointProposalConfig, EndpointScope
from groove_serpent.errors import ProjectValidationError
from groove_serpent.media import sha256_file
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.project_io import (
    load_project,
    save_project,
)
from groove_serpent.publication import canonical_json_sha256
from groove_serpent.review_server import ReviewServer


ENDPOINT_INTENT = "end-at-wanted-music-remove-lead-in-and-runout"


def _project(source_path: Path) -> Project:
    metadata = source_path.stat()
    return Project(
        source=AudioSource(
            path=source_path.name,
            filename=source_path.name,
            size_bytes=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
            duration_seconds=10.0,
            sample_rate=1_000,
            channels=2,
            codec_name="flac",
            bits_per_raw_sample=16,
            sample_format="s16",
            sample_count=10_000,
            sha256=sha256_file(source_path),
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
        metadata={"side": "A"},
    )


def _proposal(
    state: dict[str, object],
    *,
    status: str = "proposed",
    digest: str = "d" * 64,
) -> dict[str, object]:
    source = state["source"]
    assert isinstance(source, dict)
    proposed = status == "proposed"
    return {
        "schema": "groove-serpent.endpoint-proposals/1",
        "algorithm": {
            "id": "groove-serpent.multimodal-endpoints/1",
            "module": "groove_serpent.endpoint_proposals",
            "module_sha256": "a" * 64,
            "app_version": "test",
            "ffmpeg_version": "test",
        },
        "project": {
            "sha256": state["project_sha256"],
            "revision": state["revision"],
            "state_sha256": state["state_sha256"],
        },
        "source": {
            "sha256": source["sha256"],
            "size_bytes": source["size_bytes"],
            "sample_rate": source["sample_rate"],
            "channels": source["channels"],
            "bits_per_raw_sample": source["bits_per_raw_sample"],
            "sample_count": source["sample_count"],
            "codec_name": source["codec_name"],
        },
        "configuration": {"values": {}, "sha256": "b" * 64},
        "snapshot": {
            "sha256": source["sha256"],
            "size_bytes": source["size_bytes"],
            "verified_copy": True,
        },
        "scopes": [
            {
                "label": "Side A",
                "scope_start_sample": 0,
                "scope_end_sample_exclusive": 10_000,
                "status": status,
                "proposed_music_start_sample": 1_200 if proposed else None,
                "proposed_music_end_sample_exclusive": 8_700 if proposed else None,
                "confidence": 0.9 if proposed else None,
                "reasons": [] if proposed else ["contradictory_endpoint_families"],
                "requires_review": True,
                "evidence": {
                    "family_candidates": {
                        "waveform_energy": {
                            "start_sample": 1_200,
                            "end_sample_exclusive": 8_700,
                        },
                        "spectral_structure": {
                            "start_sample": 1_200,
                            "end_sample_exclusive": 8_700,
                        },
                    },
                    "needle_confirmations": [],
                    "transition_context": {},
                },
            }
        ],
        "proposal_sha256": digest,
    }


def _startup_proposal(state: dict[str, object]) -> dict[str, object]:
    proposal = _proposal(state)
    module_path = endpoint_module.__file__
    assert module_path is not None
    proposal["algorithm"] = {
        "id": "groove-serpent.multimodal-endpoints/1",
        "module": "groove_serpent.endpoint_proposals",
        "module_sha256": sha256_file(Path(module_path)),
        "app_version": __version__,
        "ffmpeg_version": "test",
    }
    values = EndpointProposalConfig().to_dict()
    proposal["configuration"] = {
        "values": values,
        "sha256": canonical_json_sha256(values),
    }
    return proposal


class EndpointCliTests(unittest.TestCase):
    def test_propose_defaults_to_full_source_and_inspect_loads_strict_document(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            source = root / "side.flac"
            source.write_bytes(b"source bytes")
            project_path = root / "side.groove.json"
            save_project(_project(source), project_path)
            output_path = root / "side.endpoint-proposals.json"
            fake = {
                "proposal_sha256": "d" * 64,
                "project": {"revision": 1, "sha256": "e" * 64},
                "scopes": [
                    {
                        "label": "Side",
                        "status": "proposed",
                        "proposed_music_start_sample": 1_200,
                        "proposed_music_end_sample_exclusive": 8_700,
                        "confidence": 0.9,
                    }
                ],
            }
            stdout = io.StringIO()
            with mock.patch(
                "groove_serpent.endpoint_proposals.analyze_endpoint_proposals",
                return_value=fake,
            ) as analyze, mock.patch(
                "groove_serpent.endpoint_proposals.write_endpoint_proposal_document",
                return_value=SimpleNamespace(sha256="f" * 64),
            ) as write, redirect_stdout(stdout):
                result = main(
                    [
                        "endpoints",
                        "propose",
                        str(project_path),
                        "--output",
                        str(output_path),
                        "--json",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(stdout.getvalue()), fake)
            self.assertEqual(
                analyze.call_args.args[1],
                (EndpointScope("Side", 0, 10_000),),
            )
            write.assert_called_once_with(fake, output_path.absolute())

            stdout = io.StringIO()
            with mock.patch(
                "groove_serpent.endpoint_proposals.load_endpoint_proposal_document",
                return_value=fake,
            ) as load, redirect_stdout(stdout):
                result = main(
                    ["endpoints", "load", str(output_path), "--json"]
                )
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(stdout.getvalue()), fake)
            load.assert_called_once_with(output_path)

    def test_scope_parser_rejects_non_exact_sample_syntax(self) -> None:
        with self.assertRaises(SystemExit):
            main(
                [
                    "endpoints",
                    "propose",
                    "side.groove.json",
                    "--output",
                    "proposal.json",
                    "--scope",
                    "A|1.5|9000",
                ]
            )

    def test_review_forwards_one_resolved_startup_proposal_path(self) -> None:
        project_path = Path("side.groove.json").resolve()
        proposal_path = Path("side.endpoint-proposals.json").resolve()
        with mock.patch(
            "groove_serpent.review_server.serve_project",
            return_value=0,
        ) as serve:
            result = main(
                [
                    "review",
                    str(project_path),
                    "--endpoint-proposal",
                    str(proposal_path),
                    "--port",
                    "4321",
                    "--no-browser",
                ]
            )
        self.assertEqual(result, 0)
        serve.assert_called_once_with(
            project_path,
            port=4321,
            open_browser=False,
            endpoint_proposal_path=proposal_path,
        )


class EndpointReviewServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source_path = self.root / "side.flac"
        self.source_path.write_bytes(bytes(range(256)) * 8)
        self.project_path = self.root / "side.groove.json"
        save_project(_project(self.source_path), self.project_path)
        self.server = ReviewServer(("127.0.0.1", 0), self.project_path)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.port = self.server.server_port
        self.authority = f"{self.server.session_auth.public_host}:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": self.server.session_auth.authorization_header,
            "Host": self.authority,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def state(self) -> dict[str, object]:
        status, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200, body)
        value = json.loads(body)
        assert isinstance(value, dict)
        value["state_sha256"] = load_project(self.project_path).state_sha256
        return value

    @staticmethod
    def identity(state: dict[str, object]) -> dict[str, object]:
        receipt = state["source_receipt"]
        assert isinstance(receipt, dict)
        return {
            "expected_revision": state["revision"],
            "expected_project_sha256": state["project_sha256"],
            "expected_source_receipt": receipt["receipt"],
        }

    def propose(
        self,
        state: dict[str, object],
        proposal: dict[str, object],
    ) -> tuple[int, bytes, mock.MagicMock]:
        patcher = mock.patch(
            "groove_serpent.review_server.analyze_endpoint_proposals",
            return_value=proposal,
        )
        analyze = patcher.start()
        self.addCleanup(patcher.stop)
        status, body = self.request(
            "POST",
            "/api/endpoints/propose",
            {**self.identity(state), "scope_label": "Side A"},
        )
        return status, body, analyze

    def test_accept_requires_explicit_review_and_records_reversible_no_runout_edit(
        self,
    ) -> None:
        state = self.state()
        proposal = _proposal(state)
        original = self.project_path.read_bytes()
        with mock.patch(
            "groove_serpent.review_server.validate_endpoint_proposal_document",
            side_effect=lambda value: value,
        ):
            status, body, analyze = self.propose(state, proposal)
            self.assertEqual(status, 200, body)
            scope = analyze.call_args.args[1][0]
            self.assertEqual(scope, EndpointScope("Side A", 0, 10_000))
            self.assertEqual(self.project_path.read_bytes(), original)

            base = {
                **self.identity(state),
                "proposal_sha256": proposal["proposal_sha256"],
                "decision": "accept",
                "intent": ENDPOINT_INTENT,
                "reviewed_start": True,
                "reviewed_end": False,
            }
            status, body = self.request("POST", "/api/endpoints/accept", base)
            self.assertEqual(status, 400, body)
            self.assertEqual(self.project_path.read_bytes(), original)

            base["reviewed_end"] = True
            status, body = self.request("POST", "/api/endpoints/accept", base)
            self.assertEqual(status, 200, body)
            accepted = json.loads(body)
            self.assertEqual(accepted["accepted_start_sample"], 1_200)
            self.assertEqual(accepted["accepted_end_sample_exclusive"], 8_700)

        saved = load_project(self.project_path)
        self.assertEqual(
            [(track.start_sample, track.end_sample) for track in saved.tracks],
            [(1_200, 5_000), (5_000, 8_700)],
        )
        history = saved.edit_history[-1]
        self.assertEqual(history.action, "move_marker")
        self.assertIn("runout", history.summary)
        self.assertEqual(history.before.tracks[0].start_sample, 1_000)
        self.assertEqual(history.before.tracks[-1].end_sample, 9_000)
        self.assertEqual(history.after_sha256, saved.state_sha256)

        status, body = self.request("GET", "/api/endpoints/status")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["state"], "empty")

    def test_reject_is_explicit_non_mutating_and_clears_pending_proposal(self) -> None:
        state = self.state()
        proposal = _proposal(state)
        original = self.project_path.read_bytes()
        with mock.patch(
            "groove_serpent.review_server.validate_endpoint_proposal_document",
            side_effect=lambda value: value,
        ):
            status, body, _analyze = self.propose(state, proposal)
            self.assertEqual(status, 200, body)
            status, body = self.request(
                "POST",
                "/api/endpoints/reject",
                {
                    **self.identity(state),
                    "proposal_sha256": proposal["proposal_sha256"],
                    "decision": "reject",
                    "reason": "",
                },
            )
        self.assertEqual(status, 200, body)
        self.assertFalse(json.loads(body)["project_mutated"])
        self.assertEqual(self.project_path.read_bytes(), original)
        status, body = self.request("GET", "/api/endpoints/status")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["state"], "empty")

    def test_abstention_substitution_and_wrong_intent_never_mutate(self) -> None:
        state = self.state()
        original = self.project_path.read_bytes()
        cases = (
            (_proposal(state, status="abstained"), "d" * 64, ENDPOINT_INTENT),
            (_proposal(state), "e" * 64, ENDPOINT_INTENT),
            (_proposal(state), "d" * 64, "remove-everything-quiet"),
        )
        for proposal, submitted_digest, intent in cases:
            with self.subTest(status=proposal["scopes"][0]["status"], intent=intent):
                with mock.patch(
                    "groove_serpent.review_server.validate_endpoint_proposal_document",
                    side_effect=lambda value: value,
                ):
                    status, body, _analyze = self.propose(state, proposal)
                    self.assertEqual(status, 200, body)
                    status, body = self.request(
                        "POST",
                        "/api/endpoints/accept",
                        {
                            **self.identity(state),
                            "proposal_sha256": submitted_digest,
                            "decision": "accept",
                            "intent": intent,
                            "reviewed_start": True,
                            "reviewed_end": True,
                        },
                    )
                self.assertIn(status, {400, 409}, body)
                self.assertEqual(self.project_path.read_bytes(), original)
                self.server.endpoint_proposal = None
                self.server.endpoint_proposal_source_receipt = None

    def test_static_review_surface_exposes_explicit_endpoint_controls(self) -> None:
        status, html = self.request("GET", "/")
        self.assertEqual(status, 200, html)
        status, script = self.request("GET", "/app.js")
        self.assertEqual(status, 200, script)
        self.assertIn(b"End where the wanted music ends", html)
        self.assertIn(b"Accept reviewed no-runout endpoints", html)
        self.assertIn(b'"/api/endpoints/propose"', script)
        self.assertIn(b'"/api/endpoints/reject"', script)
        self.assertIn(b'"/api/endpoints/accept"', script)
        self.assertIn(ENDPOINT_INTENT.encode("utf-8"), script)

    def test_startup_loads_only_an_exact_actionable_sealed_proposal(self) -> None:
        state = self.state()
        proposal = _startup_proposal(state)
        proposal_path = self.root / "sealed.endpoint-proposals.json"
        original = self.project_path.read_bytes()
        with mock.patch(
            "groove_serpent.review_server.load_endpoint_proposal_document",
            return_value=proposal,
        ) as load:
            secondary = ReviewServer(
                ("127.0.0.1", 0),
                self.project_path,
                endpoint_proposal_path=proposal_path,
            )
        try:
            load.assert_called_once_with(proposal_path)
            self.assertEqual(secondary.endpoint_proposal, proposal)
            _source, source_receipt = secondary.verify_source(
                load_project(self.project_path)
            )
            self.assertEqual(
                secondary.endpoint_proposal_source_receipt,
                source_receipt["receipt"],
            )
            self.assertEqual(self.project_path.read_bytes(), original)
        finally:
            secondary.server_close()

    def test_startup_refuses_stale_abstained_or_different_code_and_config(
        self,
    ) -> None:
        state = self.state()
        proposal_path = self.root / "sealed.endpoint-proposals.json"
        original = self.project_path.read_bytes()
        cases: list[tuple[str, dict[str, object], str]] = []

        stale = _startup_proposal(state)
        stale["project"]["sha256"] = "0" * 64
        cases.append(("stale", stale, "stale"))

        abstained = _startup_proposal(state)
        abstained["scopes"][0]["status"] = "abstained"
        cases.append(("abstained", abstained, "abstained"))

        different_code = _startup_proposal(state)
        different_code["algorithm"]["module_sha256"] = "0" * 64
        cases.append(("code", different_code, "different endpoint code"))

        different_config = _startup_proposal(state)
        different_config["configuration"]["values"]["window_ms"] += 1
        cases.append(
            ("configuration", different_config, "different review configuration")
        )

        for label, proposal, message in cases:
            with self.subTest(label=label), mock.patch(
                "groove_serpent.review_server.load_endpoint_proposal_document",
                return_value=proposal,
            ):
                with self.assertRaisesRegex(ProjectValidationError, message):
                    ReviewServer(
                        ("127.0.0.1", 0),
                        self.project_path,
                        endpoint_proposal_path=proposal_path,
                    )
                self.assertEqual(self.project_path.read_bytes(), original)

    def test_startup_propagates_strict_proposal_parse_failure(self) -> None:
        proposal_path = self.root / "malformed.endpoint-proposals.json"
        original = self.project_path.read_bytes()
        with mock.patch(
            "groove_serpent.review_server.load_endpoint_proposal_document",
            side_effect=ProjectValidationError("Malformed sealed proposal."),
        ):
            with self.assertRaisesRegex(
                ProjectValidationError,
                "Malformed sealed proposal",
            ):
                ReviewServer(
                    ("127.0.0.1", 0),
                    self.project_path,
                    endpoint_proposal_path=proposal_path,
                )
        self.assertEqual(self.project_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
