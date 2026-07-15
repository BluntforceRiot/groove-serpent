from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from groove_serpent.audio_snapshot import VerifiedAudioSnapshot
from groove_serpent.errors import ProjectValidationError
from groove_serpent.media import probe_audio, sha256_file
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.project_io import load_project, save_project
from groove_serpent.restoration_workflow import (
    PREVIEW_SCHEMA,
    RECIPE_SCHEMA,
    RENDER_SCHEMA,
    SCAN_SCHEMA,
)
from groove_serpent.review_server import ReviewServer, _compact_restoration_candidate


class RestorationServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self._fake_scan_sequence = 0
        self.directory = Path(self.temporary_directory.name)
        self.source_path = self.directory / "side.flac"
        self.source_path.write_bytes(bytes(range(256)) * 16)
        source_stat = self.source_path.stat()
        project = Project(
            source=AudioSource(
                path=self.source_path.name,
                filename=self.source_path.name,
                size_bytes=source_stat.st_size,
                modified_ns=source_stat.st_mtime_ns,
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
                    title="Track one",
                    start_sample=1_000,
                    end_sample=5_000,
                    start_seconds=1.0,
                    end_seconds=5.0,
                ),
                Track(
                    number=2,
                    title="Track two",
                    start_sample=5_000,
                    end_sample=9_000,
                    start_seconds=5.0,
                    end_seconds=9.0,
                ),
            ],
        )
        self.project_path = self.directory / "side.groove.json"
        save_project(project, self.project_path)
        self._start_server()

    def _start_server(self) -> None:
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
        self._server_running = True

    def _stop_server(self) -> None:
        if not getattr(self, "_server_running", False):
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self._server_running = False

    def _restart_server(self) -> None:
        self._stop_server()
        self._start_server()

    def tearDown(self) -> None:
        self._stop_server()
        self.temporary_directory.cleanup()

    def _state(self) -> dict[str, object]:
        status, _headers, body = self._request("GET", "/api/project")
        self.assertEqual(status, 200, body)
        value = json.loads(body)
        self.assertIsInstance(value, dict)
        return value

    def _receipt(self, state: dict[str, object] | None = None) -> dict[str, object]:
        state = state or self._state()
        source_receipt = state["source_receipt"]
        assert isinstance(source_receipt, dict)
        return {
            "expected_revision": state["revision"],
            "expected_project_sha256": state["project_sha256"],
            "expected_source_receipt": source_receipt["receipt"],
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, http.client.HTTPMessage, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = dict(headers or {})
        request_headers.setdefault("Host", self.authority)
        request_headers.setdefault(
            "Authorization", self.server.session_auth.authorization_header
        )
        if payload is not None:
            request_headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        response_body = response.read()
        result = response.status, response.headers, response_body
        connection.close()
        return result

    @staticmethod
    def _wait_for_next_utc_second() -> None:
        current = int(time.time())
        deadline = time.monotonic() + 1.2
        while int(time.time()) == current and time.monotonic() < deadline:
            time.sleep(0.01)

    @staticmethod
    def _workspace_snapshot(
        workspace: Path,
    ) -> dict[str, tuple[str, int, str]]:
        result: dict[str, tuple[str, int, str]] = {}
        if not workspace.exists():
            return result
        for path in sorted(workspace.rglob("*")):
            relative = path.relative_to(workspace).as_posix()
            metadata = path.stat()
            if path.is_file():
                result[relative] = (
                    "file",
                    metadata.st_mtime_ns,
                    sha256_file(path),
                )
            else:
                result[relative] = ("directory", metadata.st_mtime_ns, "")
        return result

    @staticmethod
    def _candidates() -> list[dict[str, object]]:
        return [
            {
                "id": "clk-one",
                "type": "impulse",
                "detected_start_frame": 1_995,
                "detected_end_frame_exclusive": 1_998,
                "start_frame": 1_994,
                "end_frame_exclusive": 2_000,
                "peak_frame": 1_996,
                "channels": [0],
                "confidence": 0.91,
                "repairable": True,
                "start_seconds": 1.994,
                "end_seconds": 2.0,
            },
            {
                "id": "clk-two",
                "type": "clipped",
                "detected_start_frame": 2_095,
                "detected_end_frame_exclusive": 2_101,
                "start_frame": 2_090,
                "end_frame_exclusive": 2_110,
                "peak_frame": 2_098,
                "channels": [1],
                "confidence": 0.82,
                "repairable": True,
                "start_seconds": 2.09,
                "end_seconds": 2.11,
            },
        ]

    def test_compact_candidate_keeps_strict_json_types_and_bounds(self) -> None:
        base = self._candidates()[0]
        compact = _compact_restoration_candidate(base)
        self.assertEqual(compact["start_frame"], 1_994)
        self.assertEqual(compact["channels"], [0])

        invalid_values: list[tuple[str, object]] = [
            ("type", []),
            ("confidence", True),
            ("confidence", float("nan")),
            ("start_frame", True),
            ("end_frame_exclusive", None),
            ("peak_frame", 2_001),
            ("start_seconds", 10**1_000),
            ("end_seconds", 1.0),
            ("channels", [0, 0]),
            ("channels", [True]),
            ("repairable", 1),
        ]
        for key, value in invalid_values:
            with self.subTest(key=key, value=value):
                candidate = dict(base)
                candidate[key] = value
                with self.assertRaises(ProjectValidationError):
                    _compact_restoration_candidate(candidate)

    def _fake_scan(
        self,
        project_path: Path,
        report_path: Path,
        *,
        start_seconds: float | None,
        end_seconds: float | None,
        max_candidates: int,
        source_snapshot: VerifiedAudioSnapshot,
    ) -> dict[str, object]:
        self.assertTrue(self.server.operation_lock.locked())
        self.assertEqual(Path(project_path).resolve(), self.project_path.resolve())
        self.assertEqual(source_snapshot.live_path, self.source_path.resolve())
        self.assertNotEqual(source_snapshot.path, self.source_path.resolve())
        self.assertEqual(source_snapshot.sha256, sha256_file(self.source_path))
        self._fake_scan_sequence += 1
        report = {
            "schema": SCAN_SCHEMA,
            "created_at": (f"2026-07-11T12:00:{self._fake_scan_sequence:02d}Z"),
            "project": {
                "path": self.project_path.name,
                "sha256": sha256_file(self.project_path),
            },
            "source": {"sha256": sha256_file(self.source_path)},
            "scan": {
                "start_frame": round((start_seconds or 0.0) * 1_000),
                "end_frame_exclusive": round((end_seconds or 10.0) * 1_000),
                "start_seconds": start_seconds or 0.0,
                "end_seconds": end_seconds or 10.0,
            },
            "candidates": self._candidates()[:max_candidates],
            "summary": {
                "detected": 2,
                "retained": min(2, max_candidates),
                "truncated": max_candidates < 2,
                "clipped": 1,
                "impulse": 1,
                "repairable": min(2, max_candidates),
            },
        }
        Path(report_path).write_text(json.dumps(report), encoding="utf-8")
        return report

    def _fake_preview(
        self,
        project_path: Path,
        scan_path: Path,
        candidate_ids: list[str],
        bundle_dir: Path,
        *,
        context_seconds: float,
        source_snapshot: VerifiedAudioSnapshot,
    ) -> dict[str, object]:
        self.assertTrue(self.server.operation_lock.locked())
        self.assertEqual(source_snapshot.live_path, self.source_path.resolve())
        self.assertNotEqual(source_snapshot.path, self.source_path.resolve())
        bundle = Path(bundle_dir)
        bundle.mkdir()
        files: dict[str, dict[str, str]] = {}
        for role, content in {
            "before": b"ORIGINAL-FLAC",
            "proposed": b"PROPOSED-FLAC",
            "removed": b"REMOVED-FLAC",
        }.items():
            path = bundle / f"{role}.flac"
            path.write_bytes(content)
            files[role] = {"path": path.name, "sha256": sha256_file(path)}
        manifest = {
            "schema": PREVIEW_SCHEMA,
            "created_at": "2026-07-11T12:01:00Z",
            "candidates": candidate_ids,
            "context": {
                "start_frame": 4_000,
                "end_frame_exclusive": 6_000,
                "repair_start_in_preview": 900,
                "repair_end_in_preview_exclusive": 1_100,
                "repair_windows": [],
                "seconds": context_seconds,
            },
            "files": files,
            "audition": {
                "before_linear_gain": 1.0,
                "proposed_linear_gain": 1.0,
                "removed_linear_gain": 16.0,
            },
            "metrics": {"changed_scalar_samples": 4},
            "proof": {"source_unchanged": True},
        }
        (bundle / "preview.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def _fake_recipe(
        self,
        project_path: Path,
        scan_path: Path,
        decisions: list[dict[str, object]],
        recipe_path: Path,
        *,
        source_snapshot: VerifiedAudioSnapshot,
    ) -> dict[str, object]:
        self.assertTrue(self.server.operation_lock.locked())
        self.assertEqual(source_snapshot.live_path, self.source_path.resolve())
        self.assertNotEqual(source_snapshot.path, self.source_path.resolve())
        recipe = {
            "schema": RECIPE_SCHEMA,
            "created_at": "2026-07-11T12:02:00Z",
            "decisions": decisions,
            "summary": {
                "candidates": len(decisions),
                "approved": sum(item["decision"] == "approved" for item in decisions),
                "rejected": sum(item["decision"] == "rejected" for item in decisions),
                "protected": sum(item["decision"] == "protected" for item in decisions),
            },
        }
        Path(recipe_path).write_text(json.dumps(recipe), encoding="utf-8")
        return recipe

    def _fake_render(
        self,
        project_path: Path,
        scan_path: Path,
        recipe_path: Path,
        bundle_dir: Path,
        *,
        source_snapshot: VerifiedAudioSnapshot,
    ) -> dict[str, object]:
        self.assertTrue(self.server.operation_lock.locked())
        self.assertEqual(source_snapshot.live_path, self.source_path.resolve())
        self.assertNotEqual(source_snapshot.path, self.source_path.resolve())
        bundle = Path(bundle_dir)
        bundle.mkdir()
        restored = bundle / "restored.flac"
        restored.write_bytes(b"LOSSLESS-RESTORED-FLAC")
        receipt = {
            "schema": RENDER_SCHEMA,
            "created_at": "2026-07-11T12:03:00Z",
            "music_range": {
                "start_frame": 1_000,
                "end_frame_exclusive": 9_000,
                "sample_count": 8_000,
            },
            "repairs": [{"candidate_id": "clk-one"}],
            "protected": [{"candidate_id": "clk-two", "classification": "needle-pickup"}],
            "files": {
                "restored": {
                    "path": restored.name,
                    "sha256": sha256_file(restored),
                    "sample_count": 8_000,
                    "sample_rate": 1_000,
                    "channels": 2,
                    "bits_per_raw_sample": 16,
                }
            },
            "pcm_proof": {"outside_approved_windows_and_channels_identical": True},
            "proof": {"source_unchanged": True, "project_unchanged": True},
        }
        (bundle / "render.json").write_text(json.dumps(receipt), encoding="utf-8")
        return receipt

    def _create_scan(self) -> dict[str, object]:
        with patch(
            "groove_serpent.review_server.scan_project_clicks",
            side_effect=self._fake_scan,
        ):
            status, _headers, body = self._request(
                "POST",
                "/api/restoration/scan",
                {**self._receipt(), "max_candidates": 50},
            )
        self.assertEqual(status, 200, body)
        return json.loads(body)["scan"]

    def test_complete_review_first_workflow_dispatch_and_registered_audio(self) -> None:
        original_project = self.project_path.read_bytes()
        original_source = self.source_path.read_bytes()
        workflow_calls: dict[str, tuple[object, ...]] = {}

        def scan(*args, **kwargs):
            workflow_calls["scan"] = (*args, kwargs)
            return self._fake_scan(*args, **kwargs)

        def preview(*args, **kwargs):
            workflow_calls["preview"] = (*args, kwargs)
            return self._fake_preview(*args, **kwargs)

        def recipe(*args, **kwargs):
            workflow_calls["recipe"] = (*args, kwargs)
            return self._fake_recipe(*args, **kwargs)

        def render(*args, **kwargs):
            workflow_calls["render"] = (*args, kwargs)
            return self._fake_render(*args, **kwargs)

        receipt = self._receipt()
        with (
            patch("groove_serpent.review_server.scan_project_clicks", side_effect=scan),
            patch("groove_serpent.review_server.create_click_preview", side_effect=preview),
            patch("groove_serpent.review_server.create_restoration_recipe", side_effect=recipe),
            patch("groove_serpent.review_server.render_restored_side", side_effect=render),
        ):
            status, _headers, body = self._request(
                "POST",
                "/api/restoration/scan",
                {
                    **receipt,
                    "start_seconds": 1.0,
                    "end_seconds": 8.0,
                    "max_candidates": 25,
                },
            )
            self.assertEqual(status, 200, body)
            scan_response = json.loads(body)["scan"]
            self.assertEqual(len(scan_response["candidates"]), 2)
            self.assertNotIn("path", scan_response)

            status, _headers, body = self._request(
                "POST",
                "/api/restoration/preview",
                {
                    **receipt,
                    "scan_token": scan_response["token"],
                    "candidate_ids": ["clk-one", "clk-two"],
                    "context_seconds": 3.5,
                },
            )
            self.assertEqual(status, 200, body)
            preview_response = json.loads(body)["preview"]
            self.assertEqual(set(preview_response["audio"]), {"before", "proposed", "removed"})
            for binding in preview_response["audio"].values():
                self.assertIn("evidence_url", binding)

            decisions = [
                {"candidate_id": "clk-one", "decision": "approved"},
                {
                    "candidate_id": "clk-two",
                    "decision": "protected",
                    "classification": "needle-pickup",
                },
            ]
            status, _headers, body = self._request(
                "POST",
                "/api/restoration/recipe",
                {
                    **receipt,
                    "scan_token": scan_response["token"],
                    "decisions": decisions,
                },
            )
            self.assertEqual(status, 200, body)
            recipe_response = json.loads(body)["recipe"]

            status, _headers, body = self._request(
                "POST",
                "/api/restoration/render",
                {
                    **receipt,
                    "scan_token": scan_response["token"],
                    "recipe_token": recipe_response["token"],
                },
            )
            self.assertEqual(status, 200, body)
            render_response = json.loads(body)["render"]
            self.assertNotIn("path", render_response["restored"])
            self.assertTrue(
                render_response["pcm_proof"]["outside_approved_windows_and_channels_identical"]
            )

        self.assertEqual(set(workflow_calls), {"scan", "preview", "recipe", "render"})
        workflow_snapshots: list[VerifiedAudioSnapshot] = []
        workflow_kwargs: dict[str, dict[str, object]] = {}
        for name, call in workflow_calls.items():
            kwargs = dict(call[-1])
            snapshot = kwargs.pop("source_snapshot")
            self.assertIsInstance(snapshot, VerifiedAudioSnapshot)
            assert isinstance(snapshot, VerifiedAudioSnapshot)
            workflow_snapshots.append(snapshot)
            workflow_kwargs[name] = kwargs
        self.assertTrue(
            all(
                snapshot.path == workflow_snapshots[0].path
                and snapshot.live_path == workflow_snapshots[0].live_path
                and snapshot.sha256 == workflow_snapshots[0].sha256
                for snapshot in workflow_snapshots
            )
        )
        self.assertEqual(
            workflow_kwargs["scan"],
            {
                "start_seconds": 1.0,
                "end_seconds": 8.0,
                "max_candidates": 25,
            },
        )
        self.assertEqual(workflow_calls["preview"][2], ["clk-one", "clk-two"])
        self.assertEqual(workflow_kwargs["preview"], {"context_seconds": 3.5})
        self.assertEqual(workflow_kwargs["recipe"], {})
        self.assertEqual(workflow_kwargs["render"], {})
        self.assertEqual(workflow_calls["recipe"][2], decisions)
        self.assertEqual(
            Path(workflow_calls["render"][1]).resolve(),
            Path(workflow_calls["preview"][1]).resolve(),
        )
        self.assertEqual(
            Path(workflow_calls["render"][2]).resolve(),
            Path(workflow_calls["recipe"][3]).resolve(),
        )
        for call in workflow_calls.values():
            for value in call[:-1]:
                if isinstance(value, Path) and value.resolve() != self.project_path.resolve():
                    self.assertTrue(
                        value.resolve().is_relative_to(self.server.restoration_workspace)
                    )

        before_url = preview_response["audio"]["before"]["url"]
        status, headers, ranged = self._request("GET", before_url, headers={"Range": "bytes=1-4"})
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Type"], "audio/flac")
        self.assertEqual(ranged, b"RIGI")

        status, _headers, _body = self._request(
            "GET", f"/api/restoration/audio/{scan_response['token']}"
        )
        self.assertEqual(status, 404)
        status, _headers, _body = self._request("GET", "/api/restoration/audio/../../side.flac")
        self.assertEqual(status, 404)
        evidence_url = preview_response["audio"]["before"]["evidence_url"]
        status, _headers, _body = self._request("GET", evidence_url + "?start=0")
        self.assertEqual(status, 400)
        status, _headers, _body = self._request(
            "GET", "/api/restoration/evidence/../../side.flac"
        )
        self.assertEqual(status, 404)

        status, _headers, body = self._request("GET", "/api/restoration/status")
        self.assertEqual(status, 200, body)
        status_payload = json.loads(body)
        self.assertEqual(status_payload["persistence_scope"], "verified-project-workspace")
        self.assertEqual(status_payload["current_scan"]["token"], scan_response["token"])
        self.assertEqual(status_payload["current_recipe"]["token"], recipe_response["token"])
        self.assertEqual(status_payload["current_render"]["token"], render_response["token"])
        self.assertEqual(self.project_path.read_bytes(), original_project)
        self.assertEqual(self.source_path.read_bytes(), original_source)

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "FFmpeg is required for restart fidelity coverage",
    )
    def test_verified_restoration_catalog_survives_restart_and_excludes_drift(
        self,
    ) -> None:
        workspace = self.server.restoration_workspace
        self._stop_server()
        if workspace.exists():
            shutil.rmtree(workspace)

        sample_rate = 22_050
        frame_count = 22_050
        click_start = 11_000
        time_axis = np.arange(frame_count, dtype=np.float64) / sample_rate
        left = 0.20 * np.sin(2.0 * np.pi * 233.0 * time_axis)
        right = 0.18 * np.sin(2.0 * np.pi * 311.0 * time_axis + 0.2)
        pcm = np.rint(np.column_stack((left, right)) * 32_767.0).astype("<i2")
        pcm[click_start : click_start + 24, 0] = np.iinfo(np.int16).min
        completed = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "s16le",
                "-ar",
                str(sample_rate),
                "-ac",
                "2",
                "-i",
                "pipe:0",
                "-c:a",
                "flac",
                "-compression_level",
                "8",
                "-sample_fmt",
                "s16",
                str(self.source_path),
            ],
            input=pcm.tobytes(),
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            self.fail(completed.stderr.decode("utf-8", errors="replace"))
        audio = probe_audio(self.source_path, stored_path=self.source_path.name)
        assert audio.sample_count is not None
        project = Project(
            source=audio,
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(
                music_start_seconds=0.0,
                music_end_seconds=audio.duration_seconds,
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=[
                Track(
                    number=1,
                    title="Restart fidelity",
                    start_sample=0,
                    end_sample=audio.sample_count,
                    start_seconds=0.0,
                    end_seconds=audio.sample_count / audio.sample_rate,
                )
            ],
        )
        save_project(project, self.project_path)
        self._start_server()

        receipt = self._receipt()
        status, _headers, body = self._request(
            "POST",
            "/api/restoration/scan",
            {**receipt, "max_candidates": 100},
        )
        self.assertEqual(status, 200, body)
        scan = json.loads(body)["scan"]
        repairable = [
            candidate
            for candidate in scan["candidates"]
            if candidate["repairable"] and candidate["type"] == "clipped"
        ]
        self.assertTrue(repairable, scan)
        approved_id = repairable[0]["id"]

        status, _headers, body = self._request(
            "POST",
            "/api/restoration/preview",
            {
                **receipt,
                "scan_token": scan["token"],
                "candidate_ids": [approved_id],
                "context_seconds": 0.1,
            },
        )
        self.assertEqual(status, 200, body)
        preview = json.loads(body)["preview"]
        decisions = [
            {
                "candidate_id": candidate["id"],
                "decision": ("approved" if candidate["id"] == approved_id else "rejected"),
            }
            for candidate in scan["candidates"]
        ]
        status, _headers, body = self._request(
            "POST",
            "/api/restoration/recipe",
            {
                **receipt,
                "scan_token": scan["token"],
                "decisions": decisions,
            },
        )
        self.assertEqual(status, 200, body)
        recipe = json.loads(body)["recipe"]
        status, _headers, body = self._request(
            "POST",
            "/api/restoration/render",
            {
                **receipt,
                "scan_token": scan["token"],
                "recipe_token": recipe["token"],
            },
        )
        self.assertEqual(status, 200, body)
        render = json.loads(body)["render"]

        restoration_before = self._workspace_snapshot(workspace)
        project_before = (
            self.project_path.read_bytes(),
            self.project_path.stat().st_mtime_ns,
        )
        source_before = (
            self.source_path.read_bytes(),
            self.source_path.stat().st_mtime_ns,
        )
        self._restart_server()
        self.assertEqual(self._workspace_snapshot(workspace), restoration_before)
        self.assertEqual(
            (
                self.project_path.read_bytes(),
                self.project_path.stat().st_mtime_ns,
            ),
            project_before,
        )
        self.assertEqual(
            (
                self.source_path.read_bytes(),
                self.source_path.stat().st_mtime_ns,
            ),
            source_before,
        )

        status, _headers, body = self._request("GET", "/api/restoration/status")
        self.assertEqual(status, 200, body)
        restarted = json.loads(body)
        self.assertEqual(restarted["current_scan"]["token"], scan["token"])
        self.assertEqual(restarted["current_recipe"]["token"], recipe["token"])
        self.assertEqual(restarted["current_preview"]["token"], preview["token"])
        self.assertEqual(restarted["current_render"]["token"], render["token"])
        self.assertEqual(
            restarted["current_preview"]["audio"]["before"]["token"],
            preview["audio"]["before"]["token"],
        )
        self.assertEqual(
            restarted["current_preview"]["audio"]["before"]["evidence_url"],
            preview["audio"]["before"]["evidence_url"],
        )
        self.assertEqual(restarted["artifact_counts"]["stale"], 0)
        self.assertEqual(restarted["artifact_counts"]["invalid"], 0)

        before_url = restarted["current_preview"]["audio"]["before"]["url"]
        status, headers, ranged = self._request("GET", before_url, headers={"Range": "bytes=0-7"})
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Type"], "audio/flac")
        self.assertEqual(len(ranged), 8)

        workspace_before_visuals = self._workspace_snapshot(workspace)
        visual_payloads: dict[str, dict[str, object]] = {}
        for role in ("before", "proposed", "removed"):
            binding = restarted["current_preview"]["audio"][role]
            status, _headers, body = self._request(
                "GET", binding["evidence_url"]
            )
            self.assertEqual(status, 200, body)
            visual = json.loads(body)
            self.assertEqual(visual["role"], role)
            self.assertEqual(visual["preview_token"], preview["token"])
            self.assertEqual(visual["audio_token"], binding["token"])
            self.assertEqual(visual["audio_sha256"], binding["sha256"])
            self.assertEqual(visual["evidence"]["source"]["sha256"], binding["sha256"])
            self.assertTrue(visual["evidence"]["waveform"]["channels"])
            self.assertTrue(visual["evidence"]["spectrogram"]["dbfs"])
            self.assertTrue(visual["alignment"]["matched_audio_geometry"])
            self.assertNotIn(str(self.directory).casefold(), body.decode().casefold())
            visual_payloads[role] = visual
        alignment_keys = {
            "source_start_sample",
            "source_end_sample_exclusive",
            "focus_source_sample",
            "repair_start_source_sample",
            "repair_end_source_sample_exclusive",
        }
        reference_alignment = visual_payloads["before"]["alignment"]
        for payload in visual_payloads.values():
            self.assertEqual(
                {key: payload["alignment"][key] for key in alignment_keys},
                {key: reference_alignment[key] for key in alignment_keys},
            )
        self.assertEqual(
            visual_payloads["before"]["alignment"]["declared_linear_gain"],
            1.0,
        )
        self.assertEqual(
            visual_payloads["proposed"]["alignment"]["declared_linear_gain"],
            1.0,
        )
        self.assertGreater(
            visual_payloads["removed"]["alignment"]["declared_linear_gain"],
            1.0,
        )
        self.assertEqual(self._workspace_snapshot(workspace), workspace_before_visuals)

        self._wait_for_next_utc_second()
        refreshed_receipt = self._receipt()
        status, _headers, body = self._request(
            "POST",
            "/api/restoration/preview",
            {
                **refreshed_receipt,
                "scan_token": scan["token"],
                "candidate_ids": [approved_id],
                "context_seconds": 0.2,
            },
        )
        self.assertEqual(status, 200, body)
        newer_preview = json.loads(body)["preview"]
        self.assertNotEqual(newer_preview["token"], preview["token"])
        status, _headers, body = self._request("GET", "/api/restoration/status")
        coherent = json.loads(body)
        self.assertEqual(coherent["current_preview"]["token"], newer_preview["token"])
        self.assertEqual(coherent["current_scan"]["token"], scan["token"])
        self.assertEqual(coherent["current_recipe"]["token"], recipe["token"])
        self.assertEqual(coherent["current_render"]["token"], render["token"])

        self._wait_for_next_utc_second()
        status, _headers, body = self._request(
            "POST",
            "/api/restoration/scan",
            {**refreshed_receipt, "max_candidates": 100},
        )
        self.assertEqual(status, 200, body)
        newer_scan = json.loads(body)["scan"]
        self.assertNotEqual(newer_scan["token"], scan["token"])
        status, _headers, body = self._request("GET", "/api/restoration/status")
        reset = json.loads(body)
        self.assertEqual(reset["current_scan"]["token"], newer_scan["token"])
        self.assertIsNone(reset["current_recipe"])
        self.assertIsNone(reset["current_preview"])
        self.assertIsNone(reset["current_render"])

        self._stop_server()
        corrupt = workspace / f"scan-{'f' * 32}.json"
        corrupt.write_bytes(b"not-json")
        changed = load_project(self.project_path)
        changed.metadata["restart-test"] = "stale all prior artifacts"
        save_project(changed, self.project_path)
        stale_workspace_before = self._workspace_snapshot(workspace)
        stale_project_before = self.project_path.read_bytes()
        stale_source_before = self.source_path.read_bytes()
        self._start_server()
        self.assertEqual(self._workspace_snapshot(workspace), stale_workspace_before)
        self.assertEqual(self.project_path.read_bytes(), stale_project_before)
        self.assertEqual(self.source_path.read_bytes(), stale_source_before)

        status, _headers, body = self._request("GET", "/api/restoration/status")
        self.assertEqual(status, 200, body)
        diagnostic = json.loads(body)
        self.assertEqual(diagnostic["artifact_counts"]["artifacts"], 0)
        self.assertGreaterEqual(diagnostic["artifact_counts"]["stale"], 6)
        self.assertGreaterEqual(diagnostic["artifact_counts"]["invalid"], 1)
        self.assertIsNone(diagnostic["current_scan"])
        self.assertIsNone(diagnostic["current_recipe"])
        self.assertIsNone(diagnostic["current_preview"])
        self.assertIsNone(diagnostic["current_render"])
        self.assertIn(
            "invalid_json",
            diagnostic["catalog_diagnostics"]["invalid"]["by_code"],
        )
        self.assertNotIn(str(self.directory).casefold(), body.decode().casefold())

    def test_scan_rejects_unknown_fields_and_non_strict_numbers(self) -> None:
        receipt = self._receipt()
        cases = [
            {**receipt, "output_path": "../../outside.json"},
            {**receipt, "start_seconds": True},
            {**receipt, "end_seconds": "9"},
            {**receipt, "max_candidates": True},
            {**receipt, "max_candidates": 3.5},
            {**receipt, "start_seconds": 8.0, "end_seconds": 1.0},
            {**receipt, "expected_source_receipt": False},
        ]
        original_project = self.project_path.read_bytes()
        original_source = self.source_path.read_bytes()
        with patch("groove_serpent.review_server.scan_project_clicks") as workflow:
            for payload in cases:
                with self.subTest(payload=payload):
                    status, _headers, _body = self._request(
                        "POST", "/api/restoration/scan", payload
                    )
                    self.assertEqual(status, 400)
        workflow.assert_not_called()
        self.assertEqual(self.project_path.read_bytes(), original_project)
        self.assertEqual(self.source_path.read_bytes(), original_source)

    def test_scan_rejects_stale_project_and_changed_source_receipts(self) -> None:
        stale = self._state()
        project = load_project(self.project_path)
        project.metadata["note"] = "concurrent edit"
        save_project(project, self.project_path)
        with patch("groove_serpent.review_server.scan_project_clicks") as workflow:
            status, _headers, body = self._request(
                "POST", "/api/restoration/scan", self._receipt(stale)
            )
        self.assertEqual(status, 409, body)
        workflow.assert_not_called()

        current = self._state()
        stat = self.source_path.stat()
        os.utime(
            self.source_path,
            ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000),
        )
        with patch("groove_serpent.review_server.scan_project_clicks") as workflow:
            status, _headers, body = self._request(
                "POST", "/api/restoration/scan", self._receipt(current)
            )
        self.assertEqual(status, 409, body)
        self.assertIn("source verification receipt", json.loads(body)["error"].lower())
        workflow.assert_not_called()

        refreshed = self._state()
        old_stat = self.source_path.stat()
        self.source_path.write_bytes(b"x" * old_stat.st_size)
        os.utime(
            self.source_path,
            ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns),
        )
        with patch("groove_serpent.review_server.scan_project_clicks") as workflow:
            status, _headers, body = self._request(
                "POST", "/api/restoration/scan", self._receipt(refreshed)
            )
        self.assertEqual(status, 400, body)
        self.assertIn("source audio changed", json.loads(body)["error"].lower())
        workflow.assert_not_called()

    def test_candidate_ids_and_protected_decisions_are_strict(self) -> None:
        scan = self._create_scan()
        receipt = self._receipt()
        invalid_previews = [
            {"candidate_ids": ["clk-missing"]},
            {"candidate_ids": ["clk-one", "clk-one"]},
            {"candidate_ids": True},
            {"candidate_ids": ["clk-one"], "scan_path": "../../scan.json"},
            {"candidate_ids": ["clk-one"], "context_seconds": True},
        ]
        with patch("groove_serpent.review_server.create_click_preview") as workflow:
            for extra in invalid_previews:
                payload = {
                    **receipt,
                    "scan_token": scan["token"],
                    **extra,
                }
                status, _headers, _body = self._request("POST", "/api/restoration/preview", payload)
                self.assertEqual(status, 400)
        workflow.assert_not_called()

        invalid_decisions = [
            [
                {"candidate_id": "clk-one", "decision": "protected"},
                {"candidate_id": "clk-two", "decision": "rejected"},
            ],
            [
                {
                    "candidate_id": "clk-one",
                    "decision": "protected",
                    "classification": "music-transient",
                },
                {"candidate_id": "clk-two", "decision": "rejected"},
            ],
            [
                {
                    "candidate_id": "clk-one",
                    "decision": "protected",
                    "classification": ["needle-drop"],
                },
                {"candidate_id": "clk-two", "decision": "rejected"},
            ],
            [
                {"candidate_id": "clk-one", "decision": ["approved"]},
                {"candidate_id": "clk-two", "decision": "rejected"},
            ],
            [
                {
                    "candidate_id": "clk-one",
                    "decision": "approved",
                    "classification": "needle-drop",
                },
                {"candidate_id": "clk-two", "decision": "rejected"},
            ],
            [{"candidate_id": "clk-one", "decision": "approved"}],
        ]
        with patch("groove_serpent.review_server.create_restoration_recipe") as workflow:
            for decisions in invalid_decisions:
                status, _headers, _body = self._request(
                    "POST",
                    "/api/restoration/recipe",
                    {
                        **receipt,
                        "scan_token": scan["token"],
                        "decisions": decisions,
                    },
                )
                self.assertEqual(status, 400)
        workflow.assert_not_called()

        valid = [
            {
                "candidate_id": "clk-one",
                "decision": "protected",
                "classification": "needle-drop",
            },
            {"candidate_id": "clk-two", "decision": "rejected"},
        ]
        with patch(
            "groove_serpent.review_server.create_restoration_recipe",
            side_effect=self._fake_recipe,
        ) as workflow:
            status, _headers, body = self._request(
                "POST",
                "/api/restoration/recipe",
                {
                    **receipt,
                    "scan_token": scan["token"],
                    "decisions": valid,
                },
            )
        self.assertEqual(status, 200, body)
        self.assertEqual(workflow.call_count, 1)

    def test_generated_outputs_are_unique_and_manifest_paths_cannot_escape(self) -> None:
        receipt = self._receipt()
        generated: list[Path] = []

        def capture(*args, **kwargs):
            generated.append(Path(args[1]))
            return self._fake_scan(*args, **kwargs)

        with patch("groove_serpent.review_server.scan_project_clicks", side_effect=capture):
            for _ in range(2):
                status, _headers, body = self._request("POST", "/api/restoration/scan", receipt)
                self.assertEqual(status, 200, body)
        self.assertEqual(len(set(generated)), 2)
        for path in generated:
            self.assertTrue(path.is_relative_to(self.server.restoration_workspace))
            self.assertTrue(path.is_file())

        scan_token = json.loads(body)["scan"]["token"]
        outside = self.directory / "outside.flac"
        outside.write_bytes(b"outside")

        def escaping_preview(
            project_path: Path,
            scan_path: Path,
            candidate_ids: list[str],
            bundle_dir: Path,
            *,
            context_seconds: float,
            source_snapshot: VerifiedAudioSnapshot,
        ) -> dict[str, object]:
            self.assertEqual(source_snapshot.live_path, self.source_path.resolve())
            bundle = Path(bundle_dir)
            bundle.mkdir()
            manifest = {
                "schema": PREVIEW_SCHEMA,
                "files": {
                    role: {
                        "path": "../../../../outside.flac",
                        "sha256": sha256_file(outside),
                    }
                    for role in ("before", "proposed", "removed")
                },
            }
            (bundle / "preview.json").write_text(json.dumps(manifest), encoding="utf-8")
            return manifest

        with patch(
            "groove_serpent.review_server.create_click_preview",
            side_effect=escaping_preview,
        ):
            status, _headers, body = self._request(
                "POST",
                "/api/restoration/preview",
                {
                    **receipt,
                    "scan_token": scan_token,
                    "candidate_ids": ["clk-one"],
                },
            )
        self.assertEqual(status, 400, body)
        self.assertEqual(self.server.restoration_audio, {})
        self.assertEqual(list(self.server.restoration_workspace.glob("preview-*")), [])

    def test_failed_postcommit_source_lease_removes_unregistered_scan(self) -> None:
        generated: list[Path] = []

        def changed_source_after_scan(*args, **kwargs):
            generated.append(Path(args[1]))
            report = self._fake_scan(*args, **kwargs)
            self.source_path.write_bytes(self.source_path.read_bytes() + b"changed")
            return report

        with patch(
            "groove_serpent.review_server.scan_project_clicks",
            side_effect=changed_source_after_scan,
        ):
            status, _headers, body = self._request("POST", "/api/restoration/scan", self._receipt())

        self.assertEqual(status, 400, body)
        self.assertEqual(len(generated), 1)
        self.assertFalse(generated[0].exists())
        self.assertEqual(self.server.restoration_artifacts, {})


if __name__ == "__main__":
    unittest.main()
