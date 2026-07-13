from __future__ import annotations

from contextlib import contextmanager
import copy
from dataclasses import replace
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Iterator
from unittest import mock

import numpy as np

import groove_serpent.restoration_workflow as restoration_workflow
from groove_serpent.audio_snapshot import verified_audio_snapshot
from groove_serpent.errors import GrooveSerpentError
from groove_serpent.media import probe_audio, sha256_file
from groove_serpent.models import AnalysisSettings, AnalysisSummary, Project, Track
from groove_serpent.project_io import save_project
from groove_serpent.restoration_workflow import (
    REMOVED_SIGNAL_GAIN,
    _candidate_identifier,
    create_click_preview,
    create_restoration_recipe,
    render_restored_side,
    scan_project_clicks,
)


@contextmanager
def _temporarily_swap_path(live: Path, replacement: Path) -> Iterator[None]:
    backup = live.with_name(f".{live.name}.original")
    incoming = live.with_name(f".{live.name}.replacement")
    if backup.exists() or incoming.exists():
        raise AssertionError("Swap helper paths already exist.")
    shutil.copy2(replacement, incoming)
    os.replace(live, backup)
    os.replace(incoming, live)
    try:
        yield
    finally:
        live.unlink(missing_ok=True)
        os.replace(backup, live)
        incoming.unlink(missing_ok=True)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg is required",
)
class RestorationRenderTests(unittest.TestCase):
    sample_rate = 44_100
    frame_count = 35_280
    click_start = 17_000
    click_end = 17_024

    def _create_source(self, directory: Path, bits: int = 16) -> Path:
        time = np.arange(self.frame_count, dtype=np.float64) / self.sample_rate
        left = 0.24 * np.sin(2.0 * np.pi * 233.0 * time + 0.1)
        right = 0.21 * np.sin(2.0 * np.pi * 311.0 * time + 0.2)
        floating = np.column_stack((left, right))
        if bits == 16:
            pcm = np.rint(floating * 32_767.0).astype("<i2")
            minimum = np.iinfo(np.int16).min
            raw_format = "s16le"
            sample_format = "s16"
        elif bits == 24:
            pcm = (
                np.rint(floating * 8_388_607.0).astype(np.int64) << 8
            ).astype("<i4")
            minimum = -(1 << 31)
            raw_format = "s32le"
            sample_format = "s32"
        else:  # pragma: no cover - helper contract
            raise AssertionError(bits)
        pcm[:12] = minimum
        pcm[self.click_start : self.click_end, 0] = minimum
        pcm[self.click_start + 6 : self.click_end + 12, 1] = minimum
        source = directory / f"source-{bits}.flac"
        completed = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                raw_format,
                "-ar",
                str(self.sample_rate),
                "-ac",
                "2",
                "-i",
                "pipe:0",
                "-c:a",
                "flac",
                "-compression_level",
                "8",
                "-sample_fmt",
                sample_format,
                str(source),
            ],
            input=pcm.tobytes(),
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:  # pragma: no cover - diagnostic path
            self.fail(completed.stderr.decode("utf-8", errors="replace"))
        return source

    def _write_pcm(self, path: Path, pcm: np.ndarray, bits: int = 16) -> None:
        raw_format = "s16le" if bits == 16 else "s32le"
        sample_format = "s16" if bits == 16 else "s32"
        completed = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                raw_format,
                "-ar",
                str(self.sample_rate),
                "-ac",
                "2",
                "-i",
                "pipe:0",
                "-c:a",
                "flac",
                "-compression_level",
                "8",
                "-sample_fmt",
                sample_format,
                str(path),
            ],
            input=np.ascontiguousarray(pcm).tobytes(),
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:  # pragma: no cover - diagnostic path
            self.fail(completed.stderr.decode("utf-8", errors="replace"))

    def _decode(self, path: Path, bits: int = 16) -> np.ndarray:
        raw_format = "s16le" if bits == 16 else "s32le"
        dtype = "<i2" if bits == 16 else "<i4"
        completed = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-f",
                raw_format,
                "pipe:1",
            ],
            capture_output=True,
            check=True,
        )
        return np.frombuffer(completed.stdout, dtype=dtype).reshape(-1, 2).copy()

    def _fixture(self, directory: Path, bits: int = 16) -> dict[str, object]:
        source = self._create_source(directory, bits)
        audio = probe_audio(source, stored_path=source.name)
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
                    title="Synthetic",
                    start_sample=0,
                    end_sample=audio.sample_count,
                    start_seconds=0.0,
                    end_seconds=audio.sample_count / audio.sample_rate,
                )
            ],
        )
        project_path = directory / "source.groove.json"
        save_project(project, project_path)
        scan_path = directory / "scan.json"
        scan = scan_project_clicks(project_path, scan_path, max_candidates=100)
        central = [
            item
            for item in scan["candidates"]
            if item["type"] == "clipped"
            and item["detected_start_frame"] < self.click_end + 12
            and item["detected_end_frame_exclusive"] > self.click_start
        ]
        self.assertEqual(sorted(item["channels"] for item in central), [[0], [1]])
        approved = next(item for item in central if item["channels"] == [0])
        protected = next(item for item in central if item["channels"] == [1])
        decisions = []
        for candidate in scan["candidates"]:
            if candidate["id"] == approved["id"]:
                decisions.append(
                    {"candidate_id": candidate["id"], "decision": "approved"}
                )
            elif candidate["id"] == protected["id"]:
                decisions.append(
                    {
                        "candidate_id": candidate["id"],
                        "decision": "protected",
                        "classification": "needle-pickup",
                    }
                )
            else:
                decisions.append(
                    {"candidate_id": candidate["id"], "decision": "rejected"}
                )
        recipe_path = directory / "recipe.json"
        create_restoration_recipe(
            project_path,
            scan_path,
            decisions,
            recipe_path,
        )
        return {
            "source": source,
            "project": project_path,
            "scan_path": scan_path,
            "scan": scan,
            "approved": approved,
            "protected": protected,
            "decisions": decisions,
            "recipe": recipe_path,
        }

    def test_full_render_changes_only_approved_channels_and_protects_needle_event(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            fixture = self._fixture(directory)
            source = fixture["source"]
            project = fixture["project"]
            scan_path = fixture["scan_path"]
            recipe = fixture["recipe"]
            approved = fixture["approved"]
            protected = fixture["protected"]
            assert isinstance(source, Path)
            assert isinstance(project, Path)
            assert isinstance(scan_path, Path)
            assert isinstance(recipe, Path)
            assert isinstance(approved, dict)
            assert isinstance(protected, dict)
            input_hashes = {
                path: sha256_file(path) for path in (source, project, scan_path, recipe)
            }

            bundle = directory / "restored"
            receipt = render_restored_side(project, scan_path, recipe, bundle)
            original = self._decode(source)
            restored = self._decode(bundle / "restored.flac")
            allowed = np.zeros(original.shape, dtype=np.bool_)
            for repair in receipt["repairs"]:
                allowed[
                    repair["start_frame"] : repair["end_frame_exclusive"],
                    repair["channels"],
                ] = True

            self.assertTrue(np.array_equal(original[~allowed], restored[~allowed]))
            self.assertGreater(np.count_nonzero(original[allowed] != restored[allowed]), 0)
            self.assertTrue(
                np.array_equal(
                    original[
                        protected["start_frame"] : protected["end_frame_exclusive"],
                        protected["channels"],
                    ],
                    restored[
                        protected["start_frame"] : protected["end_frame_exclusive"],
                        protected["channels"],
                    ],
                )
            )
            self.assertEqual(receipt["repairs"][0]["candidate_id"], approved["id"])
            self.assertEqual(
                receipt["protected"],
                [
                    {
                        "candidate_id": protected["id"],
                        "classification": "needle-pickup",
                    }
                ],
            )
            self.assertTrue(all(receipt["proof"].values()))
            self.assertTrue(all(receipt["pcm_proof"][key] for key in (
                "outside_approved_windows_and_channels_identical",
                "approved_patches_match_receipt_hashes",
            )))
            output_probe = probe_audio(bundle / "restored.flac")
            self.assertEqual(output_probe.sample_count, original.shape[0])
            self.assertEqual(output_probe.bits_per_raw_sample, 16)
            self.assertEqual(
                json.loads((bundle / "render.json").read_text(encoding="utf-8"))[
                    "schema"
                ],
                receipt["schema"],
            )
            for path, digest in input_hashes.items():
                self.assertEqual(sha256_file(path), digest)

            with self.assertRaisesRegex(GrooveSerpentError, "already exists"):
                render_restored_side(project, scan_path, recipe, bundle)

    def test_removed_preview_is_the_declared_audition_residue(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            fixture = self._fixture(directory)
            approved = fixture["approved"]
            assert isinstance(approved, dict)
            bundle = directory / "preview"
            manifest = create_click_preview(
                fixture["project"],
                fixture["scan_path"],
                approved["id"],
                bundle,
                context_seconds=0.1,
            )
            before = self._decode(bundle / "before.flac")
            proposed = self._decode(bundle / "proposed.flac")
            removed = self._decode(bundle / "removed.flac")
            difference = (
                (before.astype(np.int64) - proposed.astype(np.int64))
                * int(REMOVED_SIGNAL_GAIN)
            )
            expected = np.clip(
                difference, np.iinfo(np.int16).min, np.iinfo(np.int16).max
            ).astype(np.int16)

            self.assertTrue(np.array_equal(removed, expected))
            self.assertGreater(np.count_nonzero(removed), 0)
            self.assertEqual(manifest["audition"]["before_linear_gain"], 1.0)
            self.assertEqual(manifest["audition"]["proposed_linear_gain"], 1.0)
            self.assertEqual(
                manifest["audition"]["removed_linear_gain"], REMOVED_SIGNAL_GAIN
            )
            self.assertTrue(manifest["audition"]["matched_original_level"])

    def test_full_restored_name_requires_complete_untruncated_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            fixture = self._fixture(directory)
            original_scan = json.loads(
                Path(fixture["scan_path"]).read_text(encoding="utf-8")
            )
            cases = []

            partial = json.loads(json.dumps(original_scan))
            partial.pop("coverage", None)
            partial["scan"]["start_frame"] = 1
            partial["scan"]["start_seconds"] = 1 / self.sample_rate
            cases.append(("partial", partial))

            truncated = json.loads(json.dumps(original_scan))
            truncated.pop("coverage", None)
            truncated["summary"]["detected"] = (
                truncated["summary"]["retained"] + 1
            )
            truncated["summary"]["truncated"] = True
            cases.append(("truncated", truncated))

            for label, scan_payload in cases:
                with self.subTest(label=label):
                    scan_path = directory / f"{label}-scan.json"
                    scan_path.write_text(
                        json.dumps(scan_payload, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    recipe_path = directory / f"{label}-recipe.json"
                    recipe = create_restoration_recipe(
                        fixture["project"],
                        scan_path,
                        fixture["decisions"],
                        recipe_path,
                    )
                    self.assertEqual(
                        recipe["coverage"]["restoration_status"], "partial"
                    )
                    output = directory / f"{label}-output"
                    with self.assertRaisesRegex(
                        GrooveSerpentError, "full, untruncated scan"
                    ):
                        render_restored_side(
                            fixture["project"],
                            scan_path,
                            recipe_path,
                            output,
                        )
                    self.assertFalse(output.exists())

    def test_full_render_preserves_24_bit_pcm_format_and_outside_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            fixture = self._fixture(directory, bits=24)
            bundle = directory / "restored-24"
            receipt = render_restored_side(
                fixture["project"],
                fixture["scan_path"],
                fixture["recipe"],
                bundle,
            )
            original = self._decode(fixture["source"], bits=24)
            restored = self._decode(bundle / "restored.flac", bits=24)
            allowed = np.zeros(original.shape, dtype=np.bool_)
            for repair in receipt["repairs"]:
                allowed[
                    repair["start_frame"] : repair["end_frame_exclusive"],
                    repair["channels"],
                ] = True

            self.assertTrue(np.array_equal(original[~allowed], restored[~allowed]))
            self.assertGreater(np.count_nonzero(original[allowed] != restored[allowed]), 0)
            output_probe = probe_audio(bundle / "restored.flac")
            self.assertEqual(output_probe.bits_per_raw_sample, 24)
            self.assertEqual(output_probe.sample_count, original.shape[0])
            changed_values = restored[allowed]
            self.assertTrue(np.all(changed_values.astype(np.int64) % 256 == 0))

    def test_render_preflights_output_storage_before_audio_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            fixture = self._fixture(directory)
            source = fixture["source"]
            assert isinstance(source, Path)
            bundle = directory / "no-space-output"
            error = GrooveSerpentError(
                "Restoration render requires 1000 bytes, but only 10 bytes are available."
            )
            with mock.patch.object(
                restoration_workflow,
                "ensure_free_space",
                side_effect=error,
            ) as preflight, mock.patch.object(
                restoration_workflow,
                "_prepare_repair_patch",
            ) as prepare, mock.patch.object(
                restoration_workflow,
                "_encode_streamed_restored_flac",
            ) as encode, self.assertRaisesRegex(
                GrooveSerpentError, "only 10 bytes are available"
            ):
                render_restored_side(
                    fixture["project"],
                    fixture["scan_path"],
                    fixture["recipe"],
                    bundle,
                )

            uncompressed_music_bytes = self.frame_count * 2 * ((16 + 7) // 8)
            preflight.assert_called_once_with(
                directory.resolve(),
                max(source.stat().st_size, uncompressed_music_bytes) + 1_048_576,
                label="Restoration render",
            )
            prepare.assert_not_called()
            encode.assert_not_called()
            self.assertFalse(bundle.exists())

    def test_storage_estimate_uses_three_bytes_per_24_bit_sample(self) -> None:
        required = restoration_workflow._restoration_storage_required_bytes(
            source_size_bytes=37,
            music_frame_count=101,
            channels=2,
            bits_per_sample=24,
        )

        self.assertEqual(required, (101 * 2 * 3) + 1_048_576)

    def test_recipe_validation_is_strict_and_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            fixture = self._fixture(directory)
            valid = json.loads(
                Path(fixture["recipe"]).read_text(encoding="utf-8")
            )
            malformed_payloads = []
            extra = copy.deepcopy(valid)
            extra["decisions"][0]["unexpected"] = "field"
            malformed_payloads.append((extra, "exactly"))
            boolean_id = copy.deepcopy(valid)
            boolean_id["decisions"][0]["candidate_id"] = True
            malformed_payloads.append((boolean_id, "unknown candidate"))
            boolean_summary = copy.deepcopy(valid)
            boolean_summary["summary"]["approved"] = True
            malformed_payloads.append((boolean_summary, "summary"))
            wrong_scan = copy.deepcopy(valid)
            wrong_scan["scan"]["sha256"] = "0" * 64
            malformed_payloads.append((wrong_scan, "different scan"))

            for index, (payload, message) in enumerate(malformed_payloads):
                with self.subTest(index=index):
                    recipe_path = directory / f"malformed-{index}.json"
                    recipe_path.write_text(
                        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
                    )
                    bundle = directory / f"malformed-output-{index}"
                    with self.assertRaisesRegex(GrooveSerpentError, message):
                        render_restored_side(
                            fixture["project"],
                            fixture["scan_path"],
                            recipe_path,
                            bundle,
                        )
                    self.assertFalse(bundle.exists())

            invalid_recipe = directory / "invalid-create.json"
            invalid_decisions = copy.deepcopy(fixture["decisions"])
            invalid_decisions[0]["candidate_id"] = False
            with self.assertRaisesRegex(GrooveSerpentError, "unknown candidate"):
                create_restoration_recipe(
                    fixture["project"],
                    fixture["scan_path"],
                    invalid_decisions,
                    invalid_recipe,
                )
            self.assertFalse(invalid_recipe.exists())

    def test_no_approved_candidates_and_overlapping_repairs_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            fixture = self._fixture(directory)
            no_approval = [
                {
                    "candidate_id": item["id"],
                    "decision": "rejected",
                }
                for item in fixture["scan"]["candidates"]
            ]
            no_approval_recipe = directory / "no-approval.json"
            create_restoration_recipe(
                fixture["project"],
                fixture["scan_path"],
                no_approval,
                no_approval_recipe,
            )
            with self.assertRaisesRegex(GrooveSerpentError, "at least one"):
                render_restored_side(
                    fixture["project"],
                    fixture["scan_path"],
                    no_approval_recipe,
                    directory / "no-approval-output",
                )

            scan = copy.deepcopy(fixture["scan"])
            base = copy.deepcopy(fixture["approved"])
            base["start_frame"] += 1
            base["peak_frame"] = max(base["start_frame"], base["peak_frame"])
            base["start_seconds"] = base["start_frame"] / self.sample_rate
            base["id"] = _candidate_identifier(
                source_sha256=scan["source"]["sha256"],
                kind=base["type"],
                start_frame=base["start_frame"],
                end_frame=base["end_frame_exclusive"],
                peak_frame=base["peak_frame"],
                channels=tuple(base["channels"]),
            )
            scan["candidates"].append(base)
            overlap_scan = directory / "overlap-scan.json"
            overlap_scan.write_text(
                json.dumps(scan, indent=2) + "\n", encoding="utf-8"
            )
            overlap_decisions = []
            approved_ids = {fixture["approved"]["id"], base["id"]}
            for candidate in scan["candidates"]:
                overlap_decisions.append(
                    {
                        "candidate_id": candidate["id"],
                        "decision": (
                            "approved" if candidate["id"] in approved_ids else "rejected"
                        ),
                    }
                )
            overlap_recipe = directory / "overlap-recipe.json"
            create_restoration_recipe(
                fixture["project"],
                overlap_scan,
                overlap_decisions,
                overlap_recipe,
            )
            with self.assertRaisesRegex(GrooveSerpentError, "overlap or touch"):
                render_restored_side(
                    fixture["project"],
                    overlap_scan,
                    overlap_recipe,
                    directory / "overlap-output",
                )

    def test_recipe_is_json_only_without_audio_snapshot_or_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            fixture = self._fixture(directory)
            source = fixture["source"]
            project = fixture["project"]
            scan_path = fixture["scan_path"]
            decisions = fixture["decisions"]
            assert isinstance(source, Path)
            assert isinstance(project, Path)
            assert isinstance(scan_path, Path)
            assert isinstance(decisions, list)
            source_sha256 = sha256_file(source)
            recipe_path = directory / "json-only-recipe.json"
            with mock.patch.object(
                restoration_workflow,
                "verified_audio_snapshot",
                side_effect=AssertionError("recipe copied the full source"),
            ) as snapshot, mock.patch.object(
                restoration_workflow,
                "probe_audio",
                side_effect=AssertionError("recipe probed audio"),
            ) as probe:
                recipe = create_restoration_recipe(
                    project,
                    scan_path,
                    decisions,
                    recipe_path,
                )

            self.assertEqual(recipe["source"]["sha256"], source_sha256)
            snapshot.assert_not_called()
            probe.assert_not_called()
            self.assertEqual(sha256_file(source), source_sha256)
            self.assertEqual(list(directory.glob("groove-serpent-audio-*")), [])
            self.assertEqual(list(directory.glob("groove-serpent-input-*")), [])

            alternate = directory / "alternate.flac"
            self._write_pcm(alternate, np.roll(self._decode(source), 101, axis=0))
            original = source.read_bytes()
            source.write_bytes(alternate.read_bytes())
            try:
                with self.assertRaisesRegex(
                    GrooveSerpentError, "source no longer matches"
                ):
                    create_restoration_recipe(
                        project,
                        scan_path,
                        decisions,
                        directory / "changed-source-recipe.json",
                    )
            finally:
                source.write_bytes(original)

    def test_render_uses_one_snapshot_during_live_source_swap_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            fixture = self._fixture(directory)
            source = fixture["source"]
            project = fixture["project"]
            scan_path = fixture["scan_path"]
            recipe_path = fixture["recipe"]
            approved = fixture["approved"]
            assert isinstance(source, Path)
            assert isinstance(project, Path)
            assert isinstance(scan_path, Path)
            assert isinstance(recipe_path, Path)
            assert isinstance(approved, dict)
            source_sha256 = sha256_file(source)
            original = self._decode(source)
            alternate_pcm = np.roll(original, 101, axis=0)
            context_start = max(0, approved["start_frame"] - 512)
            context_end = min(original.shape[0], approved["end_frame_exclusive"] + 512)
            alternate_pcm[context_start:context_end] = original[context_start:context_end]
            alternate = directory / "alternate.flac"
            self._write_pcm(alternate, alternate_pcm)

            real_prepare = restoration_workflow._prepare_repair_patch
            real_encode = restoration_workflow._encode_streamed_restored_flac
            real_verify = restoration_workflow._verify_streamed_render
            observed_source_paths: list[Path] = []
            swap_context = _temporarily_swap_path(source, alternate)
            swap_active = False

            def restore_live_source() -> None:
                nonlocal swap_active
                if swap_active:
                    swap_context.__exit__(None, None, None)
                    swap_active = False

            def wrapped_prepare(*args: object, **kwargs: object) -> object:
                nonlocal swap_active
                observed_source_paths.append(Path(kwargs["source_path"]).resolve())
                if not swap_active:
                    swap_context.__enter__()
                    swap_active = True
                return real_prepare(*args, **kwargs)  # type: ignore[arg-type]

            def wrapped_encode(*args: object, **kwargs: object) -> None:
                observed_source_paths.append(Path(kwargs["source_path"]).resolve())
                real_encode(*args, **kwargs)  # type: ignore[arg-type]

            def wrapped_verify(*args: object, **kwargs: object) -> tuple[str, str]:
                observed_source_paths.append(Path(kwargs["source_path"]).resolve())
                try:
                    return real_verify(*args, **kwargs)  # type: ignore[arg-type]
                finally:
                    restore_live_source()

            bundle = directory / "snapshot-render"
            owned_snapshot = verified_audio_snapshot(source, workspace=directory)
            test_snapshot = replace(owned_snapshot, _assert_live_on_use=False)
            try:
                with (
                    mock.patch.object(
                        restoration_workflow,
                        "_prepare_repair_patch",
                        side_effect=wrapped_prepare,
                    ),
                    mock.patch.object(
                        restoration_workflow,
                        "_encode_streamed_restored_flac",
                        side_effect=wrapped_encode,
                    ),
                    mock.patch.object(
                        restoration_workflow,
                        "_verify_streamed_render",
                        side_effect=wrapped_verify,
                    ),
                ):
                    receipt = render_restored_side(
                        project,
                        scan_path,
                        recipe_path,
                        bundle,
                        source_snapshot=test_snapshot,
                    )
            finally:
                restore_live_source()
                owned_snapshot.close()

            restored = self._decode(bundle / "restored.flac")
            allowed = np.zeros(original.shape, dtype=np.bool_)
            for repair in receipt["repairs"]:
                allowed[
                    repair["start_frame"] : repair["end_frame_exclusive"],
                    repair["channels"],
                ] = True
            self.assertTrue(np.array_equal(original[~allowed], restored[~allowed]))
            self.assertEqual(len(set(observed_source_paths)), 1)
            self.assertNotEqual(observed_source_paths[0], source.resolve())
            self.assertTrue(receipt["proof"]["immutable_source_snapshot"])
            self.assertEqual(sha256_file(source), source_sha256)
            self.assertEqual(list(directory.glob("groove-serpent-audio-*")), [])
            self.assertEqual(list(directory.glob("groove-serpent-input-*")), [])

    def test_render_rejects_unrestored_live_swap_and_cleans_every_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            fixture = self._fixture(directory)
            source = fixture["source"]
            assert isinstance(source, Path)
            alternate = directory / "alternate.flac"
            self._write_pcm(alternate, np.roll(self._decode(source), 101, axis=0))
            real_encode = restoration_workflow._encode_streamed_restored_flac
            swap_context = _temporarily_swap_path(source, alternate)
            swap_active = False

            def swapped_encode(*args: object, **kwargs: object) -> None:
                nonlocal swap_active
                if not swap_active:
                    swap_context.__enter__()
                    swap_active = True
                real_encode(*args, **kwargs)  # type: ignore[arg-type]

            bundle = directory / "unrestored-swap"
            try:
                with mock.patch.object(
                    restoration_workflow,
                    "_encode_streamed_restored_flac",
                    side_effect=swapped_encode,
                ):
                    with self.assertRaisesRegex(GrooveSerpentError, "changed"):
                        render_restored_side(
                            fixture["project"],
                            fixture["scan_path"],
                            fixture["recipe"],
                            bundle,
                        )
            finally:
                if swap_active:
                    swap_context.__exit__(None, None, None)

            self.assertFalse(bundle.exists())
            self.assertEqual(list(directory.glob(".unrestored-swap.*.partial")), [])
            self.assertEqual(list(directory.glob("groove-serpent-audio-*")), [])
            self.assertEqual(list(directory.glob("groove-serpent-input-*")), [])

    def test_staged_render_failure_leaves_no_visible_or_partial_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            fixture = self._fixture(directory)
            bundle = directory / "failed-render"
            with mock.patch(
                "groove_serpent.restoration_workflow._encode_streamed_restored_flac",
                side_effect=GrooveSerpentError("injected encoder failure"),
            ):
                with self.assertRaisesRegex(GrooveSerpentError, "injected"):
                    render_restored_side(
                        fixture["project"],
                        fixture["scan_path"],
                        fixture["recipe"],
                        bundle,
                    )
            self.assertFalse(bundle.exists())
            self.assertEqual(list(directory.glob(".failed-render.*.partial")), [])
            self.assertEqual(list(directory.glob("groove-serpent-audio-*")), [])
            self.assertEqual(list(directory.glob("groove-serpent-input-*")), [])


if __name__ == "__main__":
    unittest.main()
