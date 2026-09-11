from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from groove_serpent.album import AlbumProject, AlbumSide, save_album_project
from groove_serpent.cli import main
from groove_serpent.cache_storage import acquire_snapshot_lease
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.project_io import load_project, save_project


def _analyzed_project() -> Project:
    return Project(
        source=AudioSource(
            path="side.flac",
            filename="side.flac",
            size_bytes=100,
            modified_ns=1,
            duration_seconds=1.0,
            sample_rate=1_000,
            channels=2,
            codec_name="flac",
            bits_per_raw_sample=24,
            sample_format="s32",
            sample_count=1_000,
            sha256="1" * 64,
        ),
        settings=AnalysisSettings(min_track_seconds=0.1),
        analysis=AnalysisSummary(
            music_start_seconds=0.0,
            music_end_seconds=1.0,
            noise_floor_db=-60.0,
            silence_threshold_db=-54.0,
            active_threshold_db=-42.0,
            envelope_window_seconds=0.05,
        ),
        tracks=[
            Track(
                number=1,
                title="Track",
                start_sample=0,
                end_sample=1_000,
                start_seconds=0.0,
                end_seconds=1.0,
            )
        ],
    )


class CliTests(unittest.TestCase):
    def _assert_output_redirect_rejected(self, root: Path, redirect: Path) -> None:
        project_path = root / "side.groove.json"
        album_path = root / "album.groove.json"
        save_project(_analyzed_project(), project_path)
        save_album_project(
            AlbumProject(metadata={}, sides=[AlbumSide("A", 1, project_path.name)]),
            album_path,
        )
        target = redirect.resolve()
        sentinel = target / "preserved.txt"
        sentinel.write_bytes(b"preserve existing destination contents")
        project_before = project_path.read_bytes()
        album_before = album_path.read_bytes()
        commands = (
            (
                ["export", str(project_path), "--output-dir", str(redirect / "batch"),
                 "--formats", "flac"],
                "groove_serpent.exporter.capture_file_receipt",
            ),
            (
                ["album", "export", str(album_path), "--output-dir", str(redirect / "batch"),
                 "--formats", "flac"],
                "groove_serpent.album.capture_file_receipt",
            ),
            (
                ["click-scan", str(project_path), "--report", str(redirect / "scan.json")],
                "groove_serpent.restoration_workflow._prepare_restoration_inputs",
            ),
            (
                ["click-preview", str(project_path), str(root / "scan.json"),
                 "--candidate", "clk-0123456789abcdef0123", "--bundle", str(redirect / "preview")],
                "groove_serpent.restoration_workflow._prepare_restoration_inputs",
            ),
        )
        for command, receipt_function in commands:
            with self.subTest(command=command[0:2]):
                errors = io.StringIO()
                with mock.patch(
                    receipt_function,
                    side_effect=AssertionError("Unsafe output reached source work."),
                ) as capture, redirect_stderr(errors):
                    result = main(command)
                self.assertEqual(result, 2)
                self.assertIn("symlink or reparse point", errors.getvalue())
                capture.assert_not_called()
                self.assertEqual(list(target.iterdir()), [sentinel])
                self.assertEqual(sentinel.read_bytes(), b"preserve existing destination contents")
                self.assertEqual(project_path.read_bytes(), project_before)
                self.assertEqual(album_path.read_bytes(), album_before)

    @unittest.skipUnless(os.name == "nt", "Native Windows junction regression")
    def test_commands_reject_junction_output_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            target, redirect = root / "actual", root / "redirect"
            target.mkdir()
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(redirect), str(target)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False,
            )
            if created.returncode:
                self.skipTest("This host cannot create a synthetic directory junction.")
            try:
                self.assertFalse(redirect.is_symlink())
                self._assert_output_redirect_rejected(root, redirect)
            finally:
                os.rmdir(redirect)

    def test_commands_reject_symlink_output_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            target, redirect = root / "actual", root / "redirect"
            target.mkdir()
            try:
                redirect.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"This host cannot create a synthetic symlink: {error}")
            try:
                self._assert_output_redirect_rejected(root, redirect)
            finally:
                redirect.unlink()

    def test_analyze_rejects_symlink_project_output_before_audio_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            source = root / "side.flac"
            project_path = root / "side.groove.json"
            redirect = root / "redirect.groove.json"
            save_project(_analyzed_project(), project_path)
            before = project_path.read_bytes()
            try:
                redirect.symlink_to(project_path)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"This host cannot create a synthetic symlink: {error}")
            try:
                errors = io.StringIO()
                with mock.patch("groove_serpent.cli.analyze_audio") as analyze, redirect_stderr(
                    errors
                ):
                    result = main([
                        "analyze", str(source), "--project", str(redirect), "--overwrite"
                    ])
                self.assertEqual(result, 2)
                self.assertIn("non-reparse", errors.getvalue())
                analyze.assert_not_called()
                self.assertEqual(project_path.read_bytes(), before)
                self.assertTrue(redirect.is_symlink())
            finally:
                redirect.unlink()

    def test_info_output_survives_a_redirected_legacy_windows_code_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            source = root / "side.flac"
            source_bytes = b"x" * 100
            source.write_bytes(source_bytes)
            project = _analyzed_project()
            project.source.sha256 = sha256(source_bytes).hexdigest()
            project.analyzer_baseline.source_sha256 = project.source.sha256
            project.metadata["artist"] = "Artist \u0246"
            project.tracks[0].title = "Track \u0246"
            project_path = root / "side.groove.json"
            save_project(project, project_path)

            json_bytes = io.BytesIO()
            json_stream = io.TextIOWrapper(
                json_bytes,
                encoding="cp1252",
                errors="strict",
            )
            with redirect_stdout(json_stream):
                json_result = main(["info", str(project_path), "--json"])
            json_stream.flush()
            encoded_json = json_bytes.getvalue()

            self.assertEqual(json_result, 0)
            self.assertTrue(encoded_json.isascii())
            payload = json.loads(encoded_json.decode("ascii"))
            self.assertEqual(payload["metadata"]["artist"], "Artist \u0246")
            self.assertTrue(payload["source"]["verified"])

            text_bytes = io.BytesIO()
            text_stream = io.TextIOWrapper(
                text_bytes,
                encoding="cp1252",
                errors="strict",
            )
            with redirect_stdout(text_stream):
                text_result = main(["info", str(project_path)])
            text_stream.flush()

            self.assertEqual(text_result, 0)
            self.assertIn("Track \\u0246", text_bytes.getvalue().decode("cp1252"))

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
                Path(os.path.abspath(output_dir)),
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

    def test_analyze_probes_new_destination_before_audio_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            source = root / "side.flac"
            project = root / "side.groove.json"
            errors = io.StringIO()
            with (
                mock.patch(
                    "groove_serpent.cli.probe_atomic_no_replace",
                    side_effect=OSError(95, "unsupported filesystem"),
                ),
                mock.patch("groove_serpent.cli.analyze_audio") as analyze,
                redirect_stderr(errors),
            ):
                result = main(
                    ["analyze", str(source), "--project", str(project)]
                )

            self.assertEqual(result, 2)
            self.assertIn("cannot safely create", errors.getvalue())
            analyze.assert_not_called()

    def test_analyze_overwrite_preserves_lineage_and_advances_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            source = root / "side.flac"
            source.write_bytes(b"source")
            project_path = root / "side.groove.json"
            original = _analyzed_project()
            save_project(original, project_path)
            current = load_project(project_path)
            current.metadata["artist"] = "First edit"
            save_project(current, project_path)
            before = load_project(project_path)
            replacement = _analyzed_project()
            replacement.metadata["artist"] = "Reanalyzed"
            output = io.StringIO()
            with mock.patch(
                "groove_serpent.cli.analyze_audio", return_value=replacement
            ), redirect_stdout(output):
                result = main(
                    [
                        "analyze",
                        str(source),
                        "--project",
                        str(project_path),
                        "--overwrite",
                    ]
                )

            self.assertEqual(result, 0)
            saved = load_project(project_path)
            self.assertEqual(saved.revision, before.revision + 1)
            self.assertEqual(saved.created_at, before.created_at)
            self.assertEqual(saved.metadata["artist"], "Reanalyzed")

    def test_analyze_rejects_change_during_expensive_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            root = Path(directory_value)
            source = root / "side.flac"
            source.write_bytes(b"source")
            project_path = root / "side.groove.json"
            save_project(_analyzed_project(), project_path)

            def mutate_while_analyzing(*_args: object, **_kwargs: object) -> Project:
                external = load_project(project_path)
                external.metadata["artist"] = "External writer"
                save_project(external, project_path)
                replacement = _analyzed_project()
                replacement.metadata["artist"] = "Stale analyzer"
                return replacement

            errors = io.StringIO()
            with mock.patch(
                "groove_serpent.cli.analyze_audio",
                side_effect=mutate_while_analyzing,
            ), redirect_stderr(errors):
                result = main(
                    [
                        "analyze",
                        str(source),
                        "--project",
                        str(project_path),
                        "--overwrite",
                    ]
                )

            self.assertEqual(result, 2)
            self.assertIn("changed after the caller loaded", errors.getvalue())
            self.assertEqual(
                load_project(project_path).metadata["artist"],
                "External writer",
            )

    def test_review_rejects_out_of_range_port_without_raw_overflow(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            result = main(
                ["review", "missing.groove.json", "--port", str(10**400), "--no-browser"]
            )

        self.assertEqual(result, 2)
        self.assertIn("review port", errors.getvalue())

    def test_album_review_dispatches_loopback_workbench(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            album = Path(directory_value) / "collection.groove-album.json"
            with mock.patch(
                "groove_serpent.album_review_server.serve_album_project",
                return_value=0,
            ) as serve:
                result = main(
                    [
                        "album",
                        "review",
                        str(album),
                        "--port",
                        "4321",
                        "--no-browser",
                    ]
                )
            self.assertEqual(result, 0)
            serve.assert_called_once_with(
                album.resolve(), port=4321, open_browser=False
            )

    def test_album_review_rejects_out_of_range_port_before_loading(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            result = main(
                [
                    "album",
                    "review",
                    "missing.groove-album.json",
                    "--port",
                    str(10**400),
                    "--no-browser",
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("album review port", errors.getvalue())

    def test_continuous_context_dispatches_crackle_as_distinct_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            project = directory / "side.groove.json"
            output_path = directory / "crackle-context.json"
            context = {"context_sha256": "a" * 64}
            output = io.StringIO()
            with (
                mock.patch(
                    "groove_serpent.continuous_preview_workflow."
                    "current_continuous_preview_context",
                    return_value=context,
                ) as current,
                mock.patch(
                    "groove_serpent.continuous_preview_workflow."
                    "write_continuous_expected_context",
                    return_value="b" * 64,
                ) as write,
                redirect_stdout(output),
            ):
                result = main(
                    [
                        "continuous-preview",
                        "context",
                        str(project),
                        "--kind",
                        "crackle",
                        "--output",
                        str(output_path),
                    ]
                )

            self.assertEqual(result, 0)
            current.assert_called_once_with(project, "crackle")
            write.assert_called_once_with(context, output_path)
            self.assertIn("Context SHA-256", output.getvalue())

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
                Path(os.path.abspath(report)),
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
                Path(os.path.abspath(bundle)),
                context_seconds=0.5,
            )
            self.assertIn("approval remains pending", output.getvalue())
            self.assertIn("Source audio and project were not changed", output.getvalue())

    def test_click_recipe_is_retired_in_favor_of_owner_workbench(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            project = directory / "album.groove.json"
            scan = directory / "clicks.json"
            decisions = directory / "decisions.json"
            recipe = directory / "recipe.json"
            decision_payload = [
                {"candidate_id": "clk-0123456789abcdef0123", "decision": "rejected"},
                {"candidate_id": "clk-abcdef01234567890123", "decision": "rejected"},
            ]
            decisions.write_text(
                json.dumps({"decisions": decision_payload}), encoding="utf-8"
            )
            stderr = io.StringIO()
            with mock.patch(
                "groove_serpent.restoration_workflow.create_restoration_recipe"
            ) as create, redirect_stderr(stderr):
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
            self.assertEqual(result, 2)
            create.assert_not_called()
            self.assertIn("created through the owner review workbench", stderr.getvalue())

    def test_click_recipe_does_not_process_supplied_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            decisions = directory / "decisions.json"
            decisions.write_text(
                json.dumps(
                    [{"candidate_id": "clk-0123456789abcdef0123", "decision": "approved"}]
                ),
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with mock.patch(
                "groove_serpent.restoration_workflow.create_restoration_recipe"
            ) as create, redirect_stderr(stderr):
                result = main(
                    [
                        "click-recipe",
                        str(directory / "album.groove.json"),
                        str(directory / "clicks.json"),
                        "--decisions",
                        str(decisions),
                        "--recipe",
                        str(directory / "recipe.json"),
                    ]
                )
            self.assertEqual(result, 2)
            create.assert_not_called()
            self.assertIn("created through the owner review workbench", stderr.getvalue())
            self.assertFalse((directory / "recipe.json").exists())

    def test_click_render_is_retired_in_favor_of_owner_workbench(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            project = directory / "album.groove.json"
            scan = directory / "clicks.json"
            recipe = directory / "recipe.json"
            bundle = directory / "restored"
            stderr = io.StringIO()
            with mock.patch(
                "groove_serpent.restoration_workflow.render_restored_side"
            ) as render, redirect_stderr(stderr):
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
            self.assertEqual(result, 2)
            render.assert_not_called()
            self.assertIn("owner-only action in the review workbench", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
