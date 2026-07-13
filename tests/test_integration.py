from __future__ import annotations

import shutil
import json
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from groove_serpent.analysis import analyze_audio
from groove_serpent.exporter import export_project
from groove_serpent.media import probe_audio
from groove_serpent.models import AnalysisSettings, AnalysisSummary, Project, Track
from groove_serpent.project_io import save_project
from groove_serpent.tracklist import TrackSeed


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class EndToEndTests(unittest.TestCase):
    def _create_source(self, directory: Path) -> Path:
        rate = 44_100
        rng = np.random.default_rng(7)
        segments = [
            ("gap", 1.5, 0.0),
            ("tone", 4.0, 440.0),
            ("gap", 1.2, 0.0),
            ("tone", 4.5, 660.0),
            ("gap", 1.4, 0.0),
            ("tone", 3.8, 550.0),
            ("gap", 1.2, 0.0),
        ]
        chunks = []
        for kind, duration, frequency in segments:
            sample_count = round(duration * rate)
            noise = rng.uniform(-0.0025, 0.0025, sample_count)
            if kind == "tone":
                times = np.arange(sample_count, dtype=np.float64) / rate
                signal = 0.25 * np.sin(2.0 * np.pi * frequency * times) + noise
            else:
                signal = noise
            chunks.append(signal)
        mono = np.concatenate(chunks)
        stereo = np.column_stack((mono, mono))
        pcm = np.clip(stereo * 32767.0, -32768, 32767).astype("<i2")

        wav_path = directory / "side-a.wav"
        with wave.open(str(wav_path), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(pcm.tobytes())

        flac_path = directory / "side-a.flac"
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(wav_path),
                "-c:a",
                "flac",
                str(flac_path),
            ],
            check=True,
        )
        return flac_path

    def test_analyze_and_export_matching_formats(self) -> None:
        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source = self._create_source(directory)
            project_path = directory / "side-a.groove.json"
            project = analyze_audio(
                source,
                stored_source_path=source.name,
                settings=AnalysisSettings(
                    min_gap_seconds=0.5,
                    min_track_seconds=2.0,
                    waveform_points=500,
                ),
                expected_track_count=3,
                track_seeds=[
                    TrackSeed("First", 4.0),
                    TrackSeed("Second", 4.5),
                    TrackSeed("Third", 3.8),
                ],
                metadata={"artist": "Test Artist", "album": "Test Album", "side": "A"},
            )
            project.metadata.update(
                {
                    "musicbrainz_release_id": "62d1c4ef-fc00-37af-8df7-485f6a31fcc4",
                    "musicbrainz_release_group_id": "0ef97d52-3f00-31bf-8413-f83ccb362675",
                    "musicbrainz_medium_position": "1",
                    "barcode": "012345678905",
                    "label": "Round Records",
                    "catalog_number": "RR 42",
                }
            )
            project.tracks[0].musicbrainz_recording_id = (
                "05df1765-62c0-4977-8959-bea4465e7e93"
            )
            project.tracks[0].musicbrainz_track_id = (
                "f02df099-2df0-37e3-b388-0eadc5175af3"
            )
            save_project(project, project_path)
            self.assertEqual(len(project.tracks), 3)
            self.assertAlmostEqual(project.tracks[0].end_seconds, 6.1, delta=0.35)
            self.assertAlmostEqual(project.tracks[1].end_seconds, 11.9, delta=0.35)

            output_dir = directory / "exports"
            report = export_project(
                project,
                project_path,
                output_dir,
                formats=["flac", "m4a"],
                overwrite=True,
            )
            self.assertEqual(len(report.files), 6)
            for track_number in range(1, 4):
                title = ["First", "Second", "Third"][track_number - 1]
                flac = output_dir / f"{track_number:02d} - {title}.flac"
                m4a = output_dir / f"{track_number:02d} - {title}.m4a"
                self.assertTrue(flac.is_file())
                self.assertTrue(m4a.is_file())
                flac_info = probe_audio(flac)
                m4a_info = probe_audio(m4a)
                expected_samples = (
                    project.tracks[track_number - 1].end_sample
                    - project.tracks[track_number - 1].start_sample
                )
                self.assertEqual(flac_info.sample_count, expected_samples)
                self.assertEqual(m4a_info.sample_count, expected_samples)
                self.assertAlmostEqual(
                    flac_info.duration_seconds, m4a_info.duration_seconds, delta=0.01
                )

            for tagged_path in (
                output_dir / "01 - First.flac",
                output_dir / "01 - First.m4a",
            ):
                completed = subprocess.run(
                    [
                        shutil.which("ffprobe") or "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format_tags",
                        "-of",
                        "json",
                        str(tagged_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                tags = {
                    str(key).casefold(): str(value)
                    for key, value in json.loads(completed.stdout)
                    .get("format", {})
                    .get("tags", {})
                    .items()
                }
                self.assertEqual(tags["track"], "1/3")
                self.assertEqual(tags["tracktotal"], "3")
                self.assertEqual(tags["grouping"], "Side A")
                self.assertEqual(tags["vinyl_side"], "A")
                self.assertEqual(tags["disc"], "1")
                self.assertEqual(
                    tags["musicbrainz_albumid"],
                    "62d1c4ef-fc00-37af-8df7-485f6a31fcc4",
                )
                self.assertEqual(
                    tags["musicbrainz_recordingid"],
                    "05df1765-62c0-4977-8959-bea4465e7e93",
                )
                self.assertEqual(tags["barcode"], "012345678905")
                self.assertEqual(tags["publisher"], "Round Records")
                self.assertEqual(tags["catalog_number"], "RR 42")

            corrected_dir = directory / "speed-corrected"
            corrected_report = export_project(
                project,
                project_path,
                corrected_dir,
                formats=["flac"],
                source_speed_factor=1.04,
            )
            self.assertEqual(len(corrected_report.files), 3)
            corrected_manifest = json.loads(
                Path(corrected_report.manifest_path).read_text(encoding="utf-8")
            )
            self.assertEqual(
                corrected_manifest["speed_correction"]["source_speed_factor"],
                1.04,
            )
            effective_factor = 44_100 / round(44_100 / 1.04)
            self.assertEqual(
                corrected_manifest["speed_correction"]["asetrate_hz"],
                round(44_100 / 1.04),
            )
            self.assertAlmostEqual(
                corrected_manifest["speed_correction"][
                    "effective_source_speed_factor"
                ],
                effective_factor,
            )
            corrected_paths: list[Path] = []
            for track_number, track in enumerate(project.tracks, start=1):
                corrected = corrected_dir / (
                    f"{track_number:02d} - "
                    f"{['First', 'Second', 'Third'][track_number - 1]}.flac"
                )
                corrected_paths.append(corrected)
                info = probe_audio(corrected)
                asetrate_hz = round(44_100 / 1.04)
                mapped_start = (
                    2 * track.start_sample * 44_100 + asetrate_hz
                ) // (2 * asetrate_hz)
                mapped_end = (
                    2 * track.end_sample * 44_100 + asetrate_hz
                ) // (2 * asetrate_hz)
                self.assertEqual(info.sample_count, mapped_end - mapped_start)
                completed = subprocess.run(
                    [
                        shutil.which("ffprobe") or "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format_tags",
                        "-of",
                        "json",
                        str(corrected),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                tags = {
                    str(key).casefold(): str(value)
                    for key, value in json.loads(completed.stdout)
                    .get("format", {})
                    .get("tags", {})
                    .items()
                }
                self.assertEqual(
                    tags["groove_serpent_source_speed_factor"],
                    "1.040000000",
                )
                self.assertEqual(tags["groove_serpent_asetrate_hz"], "42404")
                self.assertEqual(
                    tags["groove_serpent_effective_speed_factor"],
                    f"{effective_factor:.12f}",
                )
                self.assertIn("pitch-and-tempo together", tags["groove_serpent_speed_correction"])

            def decode_s32le(path: Path) -> bytes:
                return subprocess.run(
                    [
                        shutil.which("ffmpeg") or "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(path),
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-sn",
                        "-dn",
                        "-f",
                        "s32le",
                        "-acodec",
                        "pcm_s32le",
                        "pipe:1",
                    ],
                    check=True,
                    capture_output=True,
                ).stdout

            first_boundary = (
                2 * project.tracks[0].start_sample * 44_100 + asetrate_hz
            ) // (2 * asetrate_hz)
            last_boundary = (
                2 * project.tracks[-1].end_sample * 44_100 + asetrate_hz
            ) // (2 * asetrate_hz)
            reference_path = directory / "speed-corrected-reference.flac"
            subprocess.run(
                [
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-sn",
                    "-dn",
                    "-af",
                    (
                        f"asetrate={asetrate_hz},"
                        "aresample=44100:resampler=soxr:precision=33:cutoff=0.99,"
                        f"atrim=start_sample={first_boundary}:end_sample={last_boundary},"
                        "asettb=expr=1/44100,asetpts=N"
                    ),
                    "-ar",
                    "44100",
                    "-c:a",
                    "flac",
                    "-compression_level",
                    "8",
                    "-sample_fmt",
                    "s16",
                    str(reference_path),
                ],
                check=True,
                capture_output=True,
            )
            reference = decode_s32le(reference_path)
            reconstructed = b"".join(decode_s32le(path) for path in corrected_paths)
            self.assertEqual(reconstructed, reference)

    def test_speed_corrected_m4a_known_timestamp_residues_are_exact(self) -> None:
        """Exercise boundaries that were one presentation sample short before 0.5."""

        with tempfile.TemporaryDirectory() as directory_value:
            directory = Path(directory_value)
            source = self._create_source(directory)
            source_info = probe_audio(source, stored_path=source.name)
            rate = source_info.sample_rate
            cases = (
                (1.039482143, 190_248, 215_397),
                (1.039482143, 95_210, 145_683),
                (1.039482143, 77_920, 141_848),
                (0.75, 104_271, 178_076),
                (0.75, 128_659, 303_806),
                (1.5, 32_958, 312_897),
                (1.5, 84_196, 223_445),
            )

            for index, (factor, start, end) in enumerate(cases):
                with self.subTest(factor=factor, start=start, end=end):
                    project = Project(
                        source=source_info,
                        settings=AnalysisSettings(min_track_seconds=0.1),
                        analysis=AnalysisSummary(
                            music_start_seconds=start / rate,
                            music_end_seconds=end / rate,
                            noise_floor_db=-60.0,
                            silence_threshold_db=-54.0,
                            active_threshold_db=-42.0,
                            envelope_window_seconds=0.05,
                        ),
                        tracks=[
                            Track(
                                number=1,
                                title="Residue regression",
                                start_sample=start,
                                end_sample=end,
                                start_seconds=start / rate,
                                end_seconds=end / rate,
                            )
                        ],
                    )
                    output_dir = directory / f"corrected-m4a-{index}"
                    report = export_project(
                        project,
                        directory / f"residue-{index}.groove.json",
                        output_dir,
                        formats=["m4a"],
                        source_speed_factor=factor,
                    )
                    asetrate_hz = int(rate / factor + 0.5)
                    mapped_start = (
                        2 * start * rate + asetrate_hz
                    ) // (2 * asetrate_hz)
                    mapped_end = (
                        2 * end * rate + asetrate_hz
                    ) // (2 * asetrate_hz)
                    expected = mapped_end - mapped_start
                    exported_path = output_dir / "01 - Residue regression.m4a"

                    self.assertEqual(probe_audio(exported_path).sample_count, expected)
                    self.assertEqual(report.files[0].expected_sample_count, expected)
                    self.assertEqual(
                        report.files[0].presentation_sample_count, expected
                    )
                    manifest = json.loads(
                        Path(report.manifest_path).read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        manifest["files"][0]["expected_sample_count"], expected
                    )
                    self.assertEqual(
                        manifest["files"][0]["presentation_sample_count"],
                        expected,
                    )


if __name__ == "__main__":
    unittest.main()
