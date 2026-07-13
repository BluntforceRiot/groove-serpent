from __future__ import annotations

from contextlib import contextmanager
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
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    Project,
    Track,
)
from groove_serpent.project_io import save_project
from groove_serpent.restoration import ClickInterval
from groove_serpent.restoration_workflow import (
    _exclude_impulses_overlapping_clips,
    create_click_preview,
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
class RestorationWorkflowTests(unittest.TestCase):
    sample_rate = 44_100
    frame_count = 88_200
    central_click_start = 44_100
    central_click_end = 44_124

    def _create_source(self, directory: Path, bits: int) -> Path:
        time = np.arange(self.frame_count, dtype=np.float64) / self.sample_rate
        left = (
            0.22 * np.sin(2.0 * np.pi * 233.0 * time + 0.1)
            + 0.08 * np.sin(2.0 * np.pi * 701.0 * time + 0.4)
        )
        right = (
            0.19 * np.sin(2.0 * np.pi * 311.0 * time + 0.2)
            + 0.07 * np.sin(2.0 * np.pi * 877.0 * time + 0.6)
        )
        floating = np.column_stack((left, right))
        if bits == 16:
            pcm = np.rint(floating * 32_767.0).astype("<i2")
            pcm[:12] = np.iinfo(np.int16).min
            pcm[self.central_click_start : self.central_click_end, 0] = np.iinfo(
                np.int16
            ).min
            pcm[
                self.central_click_start + 6 : self.central_click_end + 12,
                1,
            ] = np.iinfo(np.int16).min
            raw_format = "s16le"
            sample_format = "s16"
        elif bits == 24:
            native = np.rint(floating * 8_388_607.0).astype(np.int64)
            pcm = (native << 8).astype("<i4")
            pcm[:12] = -(1 << 31)
            pcm[self.central_click_start : self.central_click_end, 0] = -(1 << 31)
            pcm[
                self.central_click_start + 6 : self.central_click_end + 12,
                1,
            ] = -(1 << 31)
            raw_format = "s32le"
            sample_format = "s32"
        else:  # pragma: no cover - test helper contract
            raise AssertionError(bits)

        source = directory / f"synthetic-{bits}.flac"
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
        self.assertEqual(probe_audio(source).bits_per_raw_sample, bits)
        return source

    def _write_pcm(self, path: Path, pcm: np.ndarray, bits: int) -> None:
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

    def _create_project(self, source: Path) -> Path:
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
                    confidence=1.0,
                )
            ],
        )
        project_path = source.with_suffix(".groove.json")
        save_project(project, project_path)
        return project_path

    def _decode(self, path: Path, bits: int) -> np.ndarray:
        raw_format = "s16le" if bits == 16 else "s32le"
        dtype = np.dtype("<i2" if bits == 16 else "<i4")
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

    def test_scan_and_preview_are_lossless_outside_selected_windows(self) -> None:
        for bits in (16, 24):
            with self.subTest(bits=bits), tempfile.TemporaryDirectory() as directory_value:
                directory = Path(directory_value)
                source = self._create_source(directory, bits)
                project = self._create_project(source)
                source_sha256 = sha256_file(source)
                project_bytes = project.read_bytes()
                report_path = directory / "clicks.json"

                report = scan_project_clicks(
                    project,
                    report_path,
                    max_candidates=100,
                )
                central = [
                    item
                    for item in report["candidates"]
                    if item["type"] == "clipped"
                    and item["detected_start_frame"] < self.central_click_end + 12
                    and item["detected_end_frame_exclusive"]
                    > self.central_click_start
                ]
                edge = next(
                    item
                    for item in report["candidates"]
                    if item["type"] == "clipped"
                    and item["detected_start_frame"] == 0
                )
                self.assertEqual(len(central), 2)
                self.assertEqual(
                    sorted(item["channels"] for item in central),
                    [[0], [1]],
                )
                self.assertTrue(all(item["repairable"] for item in central))
                self.assertFalse(edge["repairable"])
                self.assertEqual(edge["start_frame"], 0)

                bundle = directory / "preview"
                manifest = create_click_preview(
                    project,
                    report_path,
                    [item["id"] for item in central],
                    bundle,
                    context_seconds=0.1,
                )
                before = self._decode(bundle / "before.flac", bits)
                proposed = self._decode(bundle / "proposed.flac", bits)
                allowed = np.zeros(before.shape, dtype=np.bool_)
                for window in manifest["context"]["repair_windows"]:
                    allowed[
                        window["start_in_preview"] : window[
                            "end_in_preview_exclusive"
                        ],
                        window["channels"],
                    ] = True

                self.assertEqual(before.shape, proposed.shape)
                self.assertTrue(np.array_equal(before[~allowed], proposed[~allowed]))
                self.assertGreater(np.count_nonzero(before[allowed] != proposed[allowed]), 0)
                self.assertEqual(
                    manifest["metrics"]["before"]["approved_peak_absolute_sample"],
                    1 << (15 if bits == 16 else 31),
                )
                self.assertTrue(all(manifest["proof"].values()))
                self.assertEqual(manifest["approval"]["status"], "pending")
                self.assertEqual(probe_audio(bundle / "before.flac").bits_per_raw_sample, bits)
                self.assertEqual(probe_audio(bundle / "proposed.flac").bits_per_raw_sample, bits)
                self.assertEqual(sha256_file(source), source_sha256)
                self.assertEqual(project.read_bytes(), project_bytes)

                with self.assertRaisesRegex(GrooveSerpentError, "already exists"):
                    scan_project_clicks(project, report_path)
                with self.assertRaisesRegex(GrooveSerpentError, "already exists"):
                    create_click_preview(
                        project,
                        report_path,
                        [item["id"] for item in central],
                        bundle,
                    )

                incompatible = json.loads(report_path.read_text(encoding="utf-8"))
                incompatible["detector"]["clip_repair_padding_samples"] = 4
                incompatible_report = directory / "incompatible-detector.json"
                incompatible_report.write_text(
                    json.dumps(incompatible, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(GrooveSerpentError, "detector parameters"):
                    create_click_preview(
                        project,
                        incompatible_report,
                        [item["id"] for item in central],
                        directory / "incompatible-preview",
                    )

                changed_bundle = directory / "changed-project-preview"
                project.write_bytes(project_bytes + b"\n")
                with self.assertRaisesRegex(GrooveSerpentError, "project changed"):
                    create_click_preview(
                        project,
                        report_path,
                        [item["id"] for item in central],
                        changed_bundle,
                    )
                self.assertFalse(changed_bundle.exists())
                project.write_bytes(project_bytes)

                tampered = json.loads(report_path.read_text(encoding="utf-8"))
                target = next(
                    item
                    for item in tampered["candidates"]
                    if item["id"] == central[0]["id"]
                )
                target["start_frame"] += 1
                report_path.write_text(
                    json.dumps(tampered, indent=2) + "\n",
                    encoding="utf-8",
                )
                tampered_bundle = directory / "tampered-preview"
                with self.assertRaisesRegex(GrooveSerpentError, "edited after detection"):
                    create_click_preview(
                        project,
                        report_path,
                        [item["id"] for item in central],
                        tampered_bundle,
                    )
                self.assertFalse(tampered_bundle.exists())
                self.assertEqual(sha256_file(source), source_sha256)

    def test_scan_uses_one_snapshot_during_live_source_swap_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source = self._create_source(directory, 16)
            project = self._create_project(source)
            source_sha256 = sha256_file(source)
            baseline = scan_project_clicks(
                project,
                directory / "baseline.json",
                max_candidates=100,
            )
            clean = directory / "clean.flac"
            self._write_pcm(
                clean,
                np.zeros((self.frame_count, 2), dtype="<i2"),
                16,
            )
            real_decode = restoration_workflow._decode_chunks
            decode_paths: list[Path] = []

            def swapped_decode(*args: object, **kwargs: object) -> Iterator[np.ndarray]:
                decode_path = Path(args[0])
                decode_paths.append(decode_path.resolve())
                with _temporarily_swap_path(source, clean):
                    yield from real_decode(decode_path, **kwargs)  # type: ignore[arg-type]

            owned_snapshot = verified_audio_snapshot(source, workspace=directory)
            test_snapshot = replace(owned_snapshot, _assert_live_on_use=False)
            try:
                with mock.patch.object(
                    restoration_workflow,
                    "_decode_chunks",
                    side_effect=swapped_decode,
                ):
                    observed = scan_project_clicks(
                        project,
                        directory / "swapped.json",
                        max_candidates=100,
                        source_snapshot=test_snapshot,
                    )
            finally:
                owned_snapshot.close()

            self.assertEqual(observed["candidates"], baseline["candidates"])
            self.assertEqual(observed["source"]["sha256"], source_sha256)
            self.assertTrue(observed["decoder"]["immutable_source_snapshot"])
            self.assertEqual(len(set(decode_paths)), 1)
            self.assertNotEqual(decode_paths[0], source.resolve())
            self.assertEqual(sha256_file(source), source_sha256)
            self.assertEqual(list(directory.glob("groove-serpent-audio-*")), [])
            self.assertEqual(list(directory.glob("groove-serpent-input-*")), [])

            snapshot = verified_audio_snapshot(source, workspace=directory)
            try:
                borrowed = scan_project_clicks(
                    project,
                    directory / "borrowed.json",
                    max_candidates=100,
                    source_snapshot=snapshot,
                )
                snapshot.assert_snapshot_unchanged()
                self.assertEqual(borrowed["candidates"], baseline["candidates"])
            finally:
                snapshot.close()

    def test_preview_uses_snapshot_during_live_source_swap_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source = self._create_source(directory, 16)
            project = self._create_project(source)
            report = scan_project_clicks(
                project,
                directory / "clicks.json",
                max_candidates=100,
            )
            central = [
                item
                for item in report["candidates"]
                if item["type"] == "clipped"
                and item["detected_start_frame"] < self.central_click_end + 12
                and item["detected_end_frame_exclusive"] > self.central_click_start
            ]
            clean = directory / "clean.flac"
            self._write_pcm(
                clean,
                np.zeros((self.frame_count, 2), dtype="<i2"),
                16,
            )
            real_decode = restoration_workflow._decode_array
            source_decode_path: list[Path] = []

            def swapped_decode(path: Path, **kwargs: object) -> np.ndarray:
                if not source_decode_path:
                    source_decode_path.append(path.resolve())
                    with _temporarily_swap_path(source, clean):
                        return real_decode(path, **kwargs)  # type: ignore[arg-type]
                return real_decode(path, **kwargs)  # type: ignore[arg-type]

            bundle = directory / "preview-swapped"
            owned_snapshot = verified_audio_snapshot(source, workspace=directory)
            test_snapshot = replace(owned_snapshot, _assert_live_on_use=False)
            try:
                with mock.patch.object(
                    restoration_workflow,
                    "_decode_array",
                    side_effect=swapped_decode,
                ):
                    manifest = create_click_preview(
                        project,
                        directory / "clicks.json",
                        [item["id"] for item in central],
                        bundle,
                        context_seconds=0.1,
                        source_snapshot=test_snapshot,
                    )
            finally:
                owned_snapshot.close()

            original = self._decode(source, 16)
            before = self._decode(bundle / "before.flac", 16)
            start = manifest["context"]["start_frame"]
            end = manifest["context"]["end_frame_exclusive"]
            self.assertTrue(np.array_equal(before, original[start:end]))
            self.assertNotEqual(source_decode_path[0], source.resolve())
            self.assertTrue(manifest["proof"]["immutable_source_snapshot"])
            self.assertEqual(list(directory.glob("groove-serpent-audio-*")), [])
            self.assertEqual(list(directory.glob("groove-serpent-input-*")), [])

    def test_scan_range_uses_halo_and_rejects_huge_times(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source = self._create_source(directory, 16)
            project = self._create_project(source)
            source_sha256 = sha256_file(source)

            huge_report = directory / "huge.json"
            with self.assertRaisesRegex(GrooveSerpentError, "supported range"):
                scan_project_clicks(
                    project,
                    huge_report,
                    start_seconds=1e308,
                )
            self.assertFalse(huge_report.exists())

            enormous_report = directory / "enormous.json"
            with self.assertRaisesRegex(GrooveSerpentError, "finite"):
                scan_project_clicks(
                    project,
                    enormous_report,
                    start_seconds=10**10_000,
                )
            self.assertFalse(enormous_report.exists())

            partial_report = directory / "partial.json"
            report = scan_project_clicks(
                project,
                partial_report,
                start_seconds=(self.central_click_start + 10) / self.sample_rate,
                end_seconds=(self.central_click_start + 1_000) / self.sample_rate,
            )
            self.assertFalse(
                any(item["type"] == "clipped" for item in report["candidates"])
            )
            self.assertEqual(sha256_file(source), source_sha256)

    def test_overlap_suppression_is_channel_aware(self) -> None:
        clips = [ClickInterval(20, 30, 20, 1.0, (0,))]
        impulses = [
            ClickInterval(10, 11, 10, 0.8, (0,)),
            ClickInterval(22, 23, 22, 0.9, (0,)),
            ClickInterval(22, 23, 22, 0.9, (1,)),
            ClickInterval(40, 41, 40, 0.8, (0, 1)),
        ]

        retained = _exclude_impulses_overlapping_clips(impulses, clips, 2)

        self.assertEqual(
            [(item.start_sample, item.channels) for item in retained],
            [(10, (0,)), (22, (1,)), (40, (0, 1))],
        )


if __name__ == "__main__":
    unittest.main()
