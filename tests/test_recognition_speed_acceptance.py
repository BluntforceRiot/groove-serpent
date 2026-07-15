import json
import math
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from groove_serpent.recognition import _discover_fingerprint_runtime, _find_fpcalc


class RecognitionSpeedRuntimeAcceptanceTests(unittest.TestCase):
    """Exercise reviewed speed correction through real FFmpeg and Chromaprint."""

    def test_corrected_fast_capture_recovers_reference_fingerprint_geometry(self) -> None:
        status, runtime = _discover_fingerprint_runtime()
        fpcalc_value = _find_fpcalc()
        if not status.ready or runtime is None or fpcalc_value is None:
            self.skipTest("FFmpeg plus raw fpcalc are required for runtime acceptance.")

        sample_rate = 44_100
        requested_factor = 1.039482143
        correction_rate = math.floor(sample_rate / requested_factor + 0.5)
        simulated_capture_rate = math.floor(sample_rate * requested_factor + 0.5)
        duration_seconds = 48
        sample_count = sample_rate * duration_seconds
        timeline = np.arange(sample_count, dtype=np.float64) / sample_rate
        frequencies = np.array(
            [110.0, 138.591, 164.814, 220.0, 277.183, 329.628]
        )
        segments = ((timeline // 2.0).astype(np.int64) % len(frequencies))
        phase = 2.0 * np.pi * frequencies[segments] * timeline
        signal = 0.32 * np.sin(phase) + 0.13 * np.sin(2.0 * phase + 0.2)
        beat_phase = np.mod(timeline, 0.5)
        signal += (
            0.22
            * np.exp(-beat_phase * 24.0)
            * np.sin(2.0 * np.pi * 72.0 * timeline)
        )
        signal *= np.minimum(1.0, timeline / 0.1)
        signal *= np.minimum(1.0, (duration_seconds - timeline) / 0.1)
        mono = np.clip(signal * 30_000.0, -32_768, 32_767).astype("<i2")
        stereo = np.column_stack([mono, mono]).ravel()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = root / "original.wav"
            fast_capture = root / "fast-capture.wav"
            with wave.open(str(original), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(sample_rate)
                output.writeframes(stereo.tobytes())

            self._run(
                [
                    runtime.ffmpeg.path,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(original),
                    "-af",
                    (
                        f"asetrate={simulated_capture_rate},"
                        f"aresample={sample_rate}"
                    ),
                    "-c:a",
                    "pcm_s16le",
                    str(fast_capture),
                ]
            )

            variants = {
                "reference": (
                    original,
                    (
                        f"atrim=start_sample={8 * sample_rate}:"
                        f"end_sample={sample_count},asetpts=PTS-STARTPTS"
                    ),
                ),
                "corrected": (
                    fast_capture,
                    (
                        f"atrim=start_sample={8 * correction_rate},"
                        "asetpts=PTS-STARTPTS,"
                        f"asetrate={correction_rate}"
                    ),
                ),
                "raw-fast": (
                    fast_capture,
                    (
                        f"atrim=start_sample={8 * sample_rate},"
                        "asetpts=PTS-STARTPTS"
                    ),
                ),
            }
            fingerprints: dict[str, list[int]] = {}
            for name, (source, filter_graph) in variants.items():
                excerpt = root / f"{name}.wav"
                self._run(
                    [
                        runtime.ffmpeg.path,
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(source),
                        "-af",
                        filter_graph,
                        "-ac",
                        "1",
                        "-ar",
                        "11025",
                        "-c:a",
                        "pcm_s16le",
                        str(excerpt),
                    ]
                )
                result = self._run(
                    [
                        fpcalc_value,
                        "-raw",
                        "-json",
                        "-length",
                        "40",
                        str(excerpt),
                    ]
                )
                payload = json.loads(result.stdout)
                fingerprint = payload.get("fingerprint")
                self.assertIsInstance(fingerprint, list)
                fingerprints[name] = [int(value) for value in fingerprint]

        corrected_similarity = self._bit_similarity(
            fingerprints["reference"],
            fingerprints["corrected"],
        )
        raw_similarity = self._bit_similarity(
            fingerprints["reference"],
            fingerprints["raw-fast"],
        )
        self.assertGreater(corrected_similarity, 0.97)
        self.assertGreater(corrected_similarity - raw_similarity, 0.15)

    @staticmethod
    def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr[-2_000:])
        return completed

    @staticmethod
    def _bit_similarity(reference: list[int], candidate: list[int]) -> float:
        compared = min(len(reference), len(candidate))
        if compared <= 0:
            raise AssertionError("Chromaprint returned no comparable words.")
        bit_errors = sum(
            ((reference[index] ^ candidate[index]) & 0xFFFFFFFF).bit_count()
            for index in range(compared)
        )
        return 1.0 - bit_errors / (compared * 32)


if __name__ == "__main__":
    unittest.main()
