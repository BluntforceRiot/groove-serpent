from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from groove_serpent.cli import main
from groove_serpent.cache_storage import acquire_snapshot_lease


class CliTests(unittest.TestCase):
    def test_cache_status_and_clean_report_safe_lease_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value) / "cache"
            lease = acquire_snapshot_lease(
                root,
                source_sha256="c" * 64,
                source_size_bytes=128,
            )
            (lease.directory / "source.flac").write_bytes(b"x" * 128)
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(["cache", "status", "--cache-dir", str(root)])
            self.assertEqual(result, 0)
            self.assertIn("state=active, owner=live", output.getvalue())

            output = io.StringIO()
            with redirect_stdout(output):
                result = main(["cache", "clean", "--cache-dir", str(root)])
            self.assertEqual(result, 0)
            self.assertIn("Removed 0 stale snapshot", output.getvalue())
            self.assertTrue(lease.directory.exists())

            output = io.StringIO()
            with mock.patch(
                "groove_serpent.cache_storage._pid_exists", return_value=False
            ), redirect_stdout(output):
                result = main(
                    ["cache", "clean", "--cache-dir", str(root), "--json"]
                )
            self.assertEqual(result, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["schema"], "groove-serpent.cache-cleanup/1")
            self.assertEqual(payload["removed"], [str(lease.directory)])
            self.assertFalse(lease.directory.exists())
            lease.release()

    def test_cache_status_uses_project_local_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            project = Path(directory_value) / "side.groove.json"
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(
                    ["cache", "status", "--project", str(project), "--json"]
                )
            self.assertEqual(result, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(
                Path(payload["root"]),
                (
                    project.parent
                    / ".groove-serpent"
                    / "cache"
                    / "snapshots"
                ).resolve(),
            )
            self.assertEqual(payload["entries"], [])

    def test_export_dispatches_explicit_source_speed_factor(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            project_path = directory / "album.groove.json"
            output_dir = directory / "corrected"
            output = io.StringIO()
            report = SimpleNamespace(
                files=[object()],
                output_directory=str(output_dir.resolve()),
                manifest_path=str((output_dir / "groove-serpent-manifest.json").resolve()),
            )
            with mock.patch("groove_serpent.cli.load_project", return_value=object()), mock.patch(
                "groove_serpent.cli.export_project", return_value=report
            ) as export, redirect_stdout(output):
                result = main(
                    [
                        "export",
                        str(project_path),
                        "--output-dir",
                        str(output_dir),
                        "--formats",
                        "flac",
                        "--source-speed-factor",
                        "1.039",
                    ]
                )

            self.assertEqual(result, 0)
            export.assert_called_once_with(
                mock.ANY,
                project_path.resolve(),
                output_dir.resolve(),
                formats=["flac"],
                overwrite=False,
                flac_compression=8,
                aac_bitrate="256k",
                source_speed_factor=1.039,
                progress=print,
            )
            self.assertIn("Exported 1 file", output.getvalue())

    def test_analyze_refuses_to_replace_the_source_with_a_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            source = Path(directory_value) / "side.flac"
            original = b"immutable source"
            source.write_bytes(original)
            errors = io.StringIO()
            with mock.patch("groove_serpent.cli.analyze_audio") as analyze, redirect_stderr(
                errors
            ):
                result = main(
                    [
                        "analyze",
                        str(source),
                        "--project",
                        str(source),
                        "--overwrite",
                    ]
                )

            self.assertEqual(result, 2)
            self.assertIn("cannot be the source audio file", errors.getvalue())
            self.assertEqual(source.read_bytes(), original)
            analyze.assert_not_called()

    def test_analyze_rejects_unbounded_track_count_before_audio_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            source = Path(directory_value) / "side.flac"
            errors = io.StringIO()
            with mock.patch("groove_serpent.cli.analyze_audio") as analyze, redirect_stderr(
                errors
            ):
                result = main(["analyze", str(source), "--tracks", str(10**400)])

            self.assertEqual(result, 2)
            self.assertIn("--tracks must be between 1 and 1000", errors.getvalue())
            analyze.assert_not_called()

    def test_review_rejects_out_of_range_port_without_raw_overflow(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            result = main(
                ["review", "missing.groove.json", "--port", str(10**400), "--no-browser"]
            )

        self.assertEqual(result, 2)
        self.assertIn("review port", errors.getvalue())

    def test_click_scan_dispatches_review_only_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            project = directory / "album.groove.json"
            report = directory / "album.click-scan.json"
            output = io.StringIO()
            result_payload = {
                "summary": {"retained": 3, "repairable": 2},
            }
            with mock.patch(
                "groove_serpent.restoration_workflow.scan_project_clicks",
                return_value=result_payload,
            ) as scan, redirect_stdout(output):
                result = main(
                    [
                        "click-scan",
                        str(project),
                        "--report",
                        str(report),
                        "--start",
                        "10.5",
                        "--end",
                        "12.25",
                        "--max-candidates",
                        "25",
                    ]
                )

            self.assertEqual(result, 0)
            scan.assert_called_once_with(
                project.resolve(),
                report.resolve(),
                start_seconds=10.5,
                end_seconds=12.25,
                max_candidates=25,
            )
            self.assertIn("3 retained candidate", output.getvalue())
            self.assertIn("Source audio and project were not changed", output.getvalue())

    def test_click_preview_dispatches_pending_ab_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            project = directory / "album.groove.json"
            report = directory / "album.click-scan.json"
            bundle = directory / "preview"
            output = io.StringIO()
            with mock.patch(
                "groove_serpent.restoration_workflow.create_click_preview",
                return_value={"bundle_path": str(bundle.resolve())},
            ) as preview, redirect_stdout(output):
                result = main(
                    [
                        "click-preview",
                        str(project),
                        str(report),
                        "--candidate",
                        "clk-0123456789abcdef0123",
                        "--bundle",
                        str(bundle),
                        "--context",
                        "0.5",
                    ]
                )

            self.assertEqual(result, 0)
            preview.assert_called_once_with(
                project.resolve(),
                report.resolve(),
                ["clk-0123456789abcdef0123"],
                bundle.resolve(),
                context_seconds=0.5,
            )
            self.assertIn("approval remains pending", output.getvalue())
            self.assertIn("Source audio and project were not changed", output.getvalue())

    def test_click_recipe_loads_explicit_decisions_and_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            project = directory / "album.groove.json"
            scan = directory / "clicks.json"
            decisions = directory / "decisions.json"
            recipe = directory / "recipe.json"
            decision_payload = [
                {"candidate_id": "clk-0123456789abcdef0123", "decision": "approved"},
                {"candidate_id": "clk-abcdef01234567890123", "decision": "rejected"},
            ]
            decisions.write_text(
                json.dumps({"decisions": decision_payload}), encoding="utf-8"
            )
            output = io.StringIO()
            with mock.patch(
                "groove_serpent.restoration_workflow.create_restoration_recipe",
                return_value={
                    "summary": {"approved": 1, "rejected": 1, "protected": 0}
                },
            ) as create, redirect_stdout(output):
                result = main(
                    [
                        "click-recipe",
                        str(project),
                        str(scan),
                        "--decisions",
                        str(decisions),
                        "--recipe",
                        str(recipe),
                    ]
                )
            self.assertEqual(result, 0)
            create.assert_called_once_with(
                project.resolve(), scan.resolve(), decision_payload, recipe.resolve()
            )
            self.assertIn("1 approved, 1 rejected, 0 protected", output.getvalue())

    def test_click_render_dispatches_reviewed_full_side_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            project = directory / "album.groove.json"
            scan = directory / "clicks.json"
            recipe = directory / "recipe.json"
            bundle = directory / "restored"
            output = io.StringIO()
            with mock.patch(
                "groove_serpent.restoration_workflow.render_restored_side",
                return_value={"bundle_path": str(bundle.resolve()), "repairs": [{}, {}]},
            ) as render, redirect_stdout(output):
                result = main(
                    [
                        "click-render",
                        str(project),
                        str(scan),
                        str(recipe),
                        "--bundle",
                        str(bundle),
                    ]
                )
            self.assertEqual(result, 0)
            render.assert_called_once_with(
                project.resolve(), scan.resolve(), recipe.resolve(), bundle.resolve()
            )
            self.assertIn("Applied 2 explicitly approved repair", output.getvalue())


if __name__ == "__main__":
    unittest.main()
