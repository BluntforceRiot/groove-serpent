from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest import mock

from groove_serpent.album import AlbumProject, AlbumSide, pin_album_side, save_album_project
from groove_serpent.album_publication_builder import build_album_publication_plan
from groove_serpent.album_publication_executor import (
    execute_album_publication_plan,
    preflight_album_publication_plan,
)
from groove_serpent.errors import ExportError
from groove_serpent.errors import ProjectValidationError
from groove_serpent.exporter import (
    _build_command,
    _verify_staged_output,
    export_project,
    render_verified_track,
)
from groove_serpent.media import probe_audio, run_ffmpeg
from groove_serpent.models import AnalysisSettings, AnalysisSummary, Project, Track
from groove_serpent.project_io import save_project


def _project(root: Path, bits: int = 24) -> tuple[Project, Path, Path]:
    source = root / "precision.wav"
    # Actual low-order information, not a 16-bit sine padded into a 24-bit file.
    width = bits // 8
    limit = 1 << (bits - 2)
    payload = b"".join(
        ((index * 7919) % (2 * limit) - limit).to_bytes(width, "little", signed=True)
        for index in range(48_000)
    )
    with wave.open(str(source), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(width)
        handle.setframerate(48_000)
        handle.writeframes(payload)
    project = Project(
        source=probe_audio(source, stored_path=source.name),
        settings=AnalysisSettings(min_track_seconds=0.1),
        analysis=AnalysisSummary(0.0, 1.0, -60.0, -54.0, -42.0, 0.05),
        tracks=[Track(1, "Precision", 0, 48_000, 0.0, 1.0)],
    )
    path = root / "precision.groove.json"
    save_project(project, path)
    return project, path, source


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class SourceDescriptorAuthorityTests(unittest.TestCase):
    def test_side_export_refuses_false_precision_and_unknown_precision(self) -> None:
        for bits, sample_format in ((16, "s16"), (None, "s32")):
            with self.subTest(bits=bits), tempfile.TemporaryDirectory() as value:
                root = Path(value)
                project, path, source = _project(root)
                before = source.read_bytes()
                project.source.bits_per_raw_sample = bits
                project.source.sample_format = sample_format
                save_project(project, path)
                output = root / "published"
                with self.assertRaisesRegex(ExportError, "no longer matches"):
                    export_project(project, path, output, formats=("flac",))
                self.assertFalse(output.exists())
                self.assertEqual(before, source.read_bytes())

    def test_every_stream_descriptor_field_is_authoritative(self) -> None:
        from groove_serpent.media import audio_source_descriptor_mismatches

        with tempfile.TemporaryDirectory() as value:
            project, _, _ = _project(Path(value))
            source = project.source
            cases = {
                "sample_rate": 44_100, "channels": 2, "codec_name": "flac",
                "bits_per_raw_sample": 16, "sample_format": "s16",
                "sample_count": None, "size_bytes": source.size_bytes + 1,
                "sha256": "0" * 64, "duration_seconds": 2.0,
            }
            for field, changed in cases.items():
                with self.subTest(field=field):
                    self.assertIn(
                        field,
                        audio_source_descriptor_mismatches(
                            replace(source, **{field: changed}), source
                        ),
                    )
            copied = replace(source, path="elsewhere.wav", modified_ns=0, filename="snapshot.wav")
            self.assertEqual(audio_source_descriptor_mismatches(copied, source), ())

    def test_core_renderer_refuses_false_precision_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            project, _, source = _project(root)
            output = root / "downcast.flac"
            with self.assertRaisesRegex(ExportError, "precision|geometry"):
                render_verified_track(
                    source_snapshot=source, staged_path=output, track=project.tracks[0],
                    total_tracks=1, output_format="flac", expected_sample_count=48_000,
                    source_sample_rate=48_000, source_channels=1, source_bits=16,
                    flac_compression=8, aac_bitrate="256k",
                )
            self.assertFalse(output.exists())

    def test_endpoint_snapshot_rejects_false_sample_format(self) -> None:
        from groove_serpent.endpoint_proposals import _verify_snapshot_geometry

        with tempfile.TemporaryDirectory() as value:
            project, _, source = _project(Path(value))
            project.source.sample_format = "s16"
            with self.assertRaisesRegex(ProjectValidationError, "geometry"):
                _verify_snapshot_geometry(project, source)

    def test_pcm_oracle_does_not_downconvert_source_to_match_output(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            project, _, source = _project(root)
            output = root / "downcast.flac"
            run_ffmpeg(_build_command(
                source_path=source, output_path=output, track=project.tracks[0],
                total_tracks=1, output_format="flac", source_sample_rate=48_000,
                source_bits=16, overwrite=False, flac_compression=8, aac_bitrate="256k",
            ))
            # Isolate the independent PCM oracle from the new descriptor guard.
            # The real public-path regressions above do not mock either check.
            with mock.patch("groove_serpent.exporter._verify_render_source_geometry", create=True):
                with self.assertRaisesRegex(ExportError, "exact selected source PCM"):
                    _verify_staged_output(
                        staged_path=output, source_snapshot=source, track=project.tracks[0],
                        output_format="flac", expected_sample_count=48_000,
                        source_sample_rate=48_000, source_channels=1, source_bits=16,
                        source_speed_factor=None, total_tracks=1,
                    )

    def test_core_renderer_refuses_32_bit_integer_source_for_flac(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            project, _, source = _project(root, 32)
            self.assertEqual(project.source.bits_per_raw_sample, 32)
            output = root / "downcast.flac"
            with self.assertRaisesRegex(ExportError, "24 bits"):
                render_verified_track(
                    source_snapshot=source, staged_path=output, track=project.tracks[0],
                    total_tracks=1, output_format="flac", expected_sample_count=48_000,
                    source_sample_rate=48_000, source_channels=1, source_bits=32,
                    flac_compression=8, aac_bitrate="256k",
                )
            self.assertFalse(output.exists())

    def test_unified_publication_refuses_false_source_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            project, path, source = _project(root)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            project.source.bits_per_raw_sample = 16
            project.source.sample_format = "s16"
            save_project(project, path)
            album_path = root / "album.groove-album.json"
            side = AlbumSide("A", 1, path.name)
            pin_album_side(side, album_path)
            save_album_project(AlbumProject(metadata={}, sides=[side]), album_path)
            plan_path = root / "plan.json"
            build_album_publication_plan(
                album_path, plan_path,
                selected_profiles=("archival-source", "corrected-lossless"),
                restoration_mode="none",
            )
            with self.assertRaisesRegex(ExportError, "descriptor"):
                preflight_album_publication_plan(plan_path)
            output = root / "published"
            with self.assertRaisesRegex(ExportError, "descriptor"):
                execute_album_publication_plan(plan_path, output)
            self.assertFalse(output.exists())
            self.assertEqual(digest, hashlib.sha256(source.read_bytes()).hexdigest())

    def test_actual_16_and_24_bit_sources_remain_pcm_exact(self) -> None:
        for bits in (16, 24):
            with self.subTest(bits=bits), tempfile.TemporaryDirectory() as value:
                root = Path(value)
                project, path, source = _project(root, bits)
                before = source.read_bytes()
                report = export_project(project, path, root / "published", formats=("flac",))
                output = Path(report.files[0].path)
                if not output.is_absolute():
                    output = Path(report.output_directory) / output
                self.assertEqual(probe_audio(output).bits_per_raw_sample, bits)

                def decode(path: Path) -> bytes:
                    result = subprocess.run(
                        [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error",
                         "-i", str(path), "-map", "0:a:0", "-f", "s32le",
                         "-c:a", "pcm_s32le", "pipe:1"],
                        check=True, capture_output=True,
                    )
                    return result.stdout

                self.assertEqual(decode(source), decode(output))
                self.assertEqual(before, source.read_bytes())


if __name__ == "__main__":
    unittest.main()
